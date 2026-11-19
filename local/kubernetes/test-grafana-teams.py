#!/usr/bin/env python3
"""Send one authorized synthetic test through the provisioned Teams contact point."""
import json
import os
import re
import subprocess
import time
import uuid

import smoke


PROBE = r'''
import base64, json, os, urllib.error, urllib.parse, urllib.request

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

client = urllib.request.build_opener(NoRedirect)
auth = base64.b64encode((os.environ["USER_NAME"] + ":" + os.environ["PASSWORD"]).encode()).decode()
base = "http://grafana.observability.svc.cluster.local:3000"

def request(path, body=None):
    headers = {"Authorization": "Basic " + auth}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers=headers)
    with client.open(req, timeout=30) as response:
        return response.status, json.load(response)

_, exported = request("/api/v1/provisioning/contact-points/export?name=local-teams&decrypt=true&format=json")
contacts = [r for c in exported.get("contactPoints", []) for r in c.get("receivers", [])
            if r.get("uid") == "local-teams" and r.get("type") == "teams"]
if len(contacts) != 1:
    raise SystemExit("Contact point Teams provisionado não encontrado.")
settings = contacts[0].get("settings", {})
url = settings.get("url", "")
if url != os.environ["TEAMS_WEBHOOK_URL"]:
    raise SystemExit("Webhook do contact point diverge do Secret local.")
parsed = urllib.parse.urlsplit(url)
host = (parsed.hostname or "").lower()
allowed = ("office.com", "office365.com", "logic.azure.com",
           "powerautomate.com", "powerplatform.com")
if (parsed.scheme != "https" or parsed.username or parsed.password
        or not any(host.endswith("." + suffix) for suffix in allowed)):
    raise SystemExit("Webhook não passou na validação de destino HTTPS.")
settings.update({"title": "Teste sintetico ToggleMaster TC04-TEAMS-1",
                 "message": "Teste sintetico autorizado TC04-TEAMS-1; nenhum dado real."})
body = {"alert": {"labels": {"alertname": "ToggleTeamsSyntheticTest",
                             "instance": "synthetic-local"},
                  "annotations": {"summary": "Teste sintetico TC04-TEAMS-1"}},
        "receivers": [{"name": "local-teams", "grafana_managed_receiver_configs": [
            {"uid": "local-teams", "name": "local-teams", "type": "teams",
             "settings": settings}]}]}
try:
    status, result = request("/api/alertmanager/grafana/config/api/v1/receivers/test", body)
except urllib.error.HTTPError as error:
    raise SystemExit("Grafana recusou ou falhou no teste Teams; HTTP " + str(error.code)) from None
except (urllib.error.URLError, TimeoutError, ValueError, OSError):
    raise SystemExit("Teste Teams não confirmado por erro de comunicação.") from None
configs = [c for r in result.get("receivers", [])
           for c in r.get("grafana_managed_receiver_configs", [])]
if status != 200 or len(configs) != 1 or configs[0].get("status") != "ok":
    raise SystemExit("Grafana não confirmou envio ao webhook Teams.")
print("Grafana confirmou aceite HTTP do webhook Teams para TC04-TEAMS-1.", flush=True)
print("A execução do Workflow e a postagem no canal não foram confirmadas.", flush=True)
'''


def main():
    if not os.environ.get("KUBECONFIG"):
        raise RuntimeError("KUBECONFIG local obrigatório")
    server = subprocess.check_output(
        smoke.KUBECTL + ["config", "view", "--minify", "-o",
                         "jsonpath={.clusters[0].cluster.server}"], text=True
    )
    if not re.fullmatch(r"https://127\.0\.0\.1:\d+", server):
        raise RuntimeError("API Kubernetes não está no loopback")
    name = "grafana-teams-" + uuid.uuid4().hex[:10]
    env = [
        {"name": name, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}
        for name, secret, key in (
            ("USER_NAME", "grafana-admin", "username"),
            ("PASSWORD", "grafana-admin", "password"),
            ("TEAMS_WEBHOOK_URL", "notification-credentials", "teams-webhook"),
        )
    ]
    pod = {"apiVersion": "v1", "kind": "Pod",
           "metadata": {"name": name, "namespace": "observability"},
           "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                    "containers": [{"name": "probe", "image": "python:3.11-slim",
                                    "imagePullPolicy": "Never",
                                    "command": ["python3", "-u", "-c", PROBE], "env": env}]}}
    subprocess.run(smoke.KUBECTL + ["create", "-f", "-"], input=json.dumps(pod),
                   text=True, capture_output=True, check=True)
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            state = json.loads(subprocess.check_output(
                smoke.KUBECTL + ["-n", "observability", "get", "pod", name, "-o", "json"]
            ))
            phase = state["status"].get("phase")
            if phase in ("Succeeded", "Failed"):
                print(subprocess.check_output(
                    smoke.KUBECTL + ["-n", "observability", "logs", name], text=True
                ).strip())
                if phase != "Succeeded":
                    raise RuntimeError("Teste Grafana/Teams não foi confirmado")
                return
            time.sleep(3)
        raise RuntimeError("Teste Teams excedeu o prazo")
    finally:
        subprocess.run(smoke.KUBECTL + ["-n", "observability", "delete", "pod", name,
                                       "--ignore-not-found", "--wait=true"],
                       capture_output=True, check=True)


if __name__ == "__main__":
    main()
