#!/usr/bin/env python3
"""Send explicitly authorized synthetic external notifications from local kind."""
import argparse
import json
import os
import re
import subprocess
import time
import uuid

import smoke


PROBE = r'''
import base64, json, os, re, time, urllib.error, urllib.parse, urllib.request, uuid

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

client = urllib.request.build_opener(NoRedirect)

def post(url, body, headers=None):
    headers = dict(headers or {})
    headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    try:
        with client.open(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None, None

errors = []
if os.environ.get("TEST_TEAMS") == "1":
    webhook = urllib.parse.urlsplit(os.environ["TEAMS_WEBHOOK_URL"])
    host = (webhook.hostname or "").lower()
    allowed = ("office.com", "office365.com", "logic.azure.com",
               "powerautomate.com", "powerplatform.com")
    if (webhook.scheme != "https" or webhook.username or webhook.password
            or not any(host.endswith("." + suffix) for suffix in allowed)):
        print("Teams: webhook não passou na validação de destino HTTPS.", flush=True)
        errors.append("teams")
    else:
        auth = base64.b64encode(
            (os.environ["GRAFANA_USER"] + ":" + os.environ["GRAFANA_PASSWORD"]).encode()
        ).decode()
        body = {"alert": {"labels": {"alertname": "ToggleMasterSyntheticNotification"},
                           "annotations": {"summary": "Teste sintetico de notificacao"}},
                "receivers": [{"name": "synthetic-teams", "grafana_managed_receiver_configs": [
                    {"name": "synthetic-teams", "type": "teams",
                     "settings": {"url": os.environ["TEAMS_WEBHOOK_URL"]}}]}]}
        status, result = post(
            "http://grafana.observability.svc.cluster.local:3000/api/alertmanager/grafana/config/api/v1/receivers/test",
            body, {"Authorization": "Basic " + auth})
        configs = [c for r in (result or {}).get("receivers", [])
                   for c in r.get("grafana_managed_receiver_configs", [])]
        if status == 200 and len(configs) == 1 and configs[0].get("status") == "ok":
            print("Teams: teste Grafana aceito pelo endpoint configurado.", flush=True)
        else:
            print("Teams: teste Grafana falhou; HTTP Grafana: " + str(status), flush=True)
            errors.append("teams")

if not re.fullmatch(r"[a-fA-F0-9]{32}", os.environ["PAGERDUTY_INTEGRATION_KEY"]):
    print("PagerDuty: integration key não corresponde ao formato Events API v2.", flush=True)
    errors.append("pagerduty-key")
else:
    endpoint = "https://events.pagerduty.com/v2/enqueue"
    key = "toggle-local-synthetic-" + uuid.uuid4().hex
    routing_key = os.environ["PAGERDUTY_INTEGRATION_KEY"]
    trigger = {"routing_key": routing_key, "event_action": "trigger", "dedup_key": key,
               "payload": {"summary": "Teste sintetico ToggleMaster",
                           "source": "toggle-local", "severity": "info"}}
    resolution = {"routing_key": routing_key, "event_action": "resolve", "dedup_key": key}
    status = result = resolve_status = resolve_result = None
    try:
        status, result = post(endpoint, trigger)
        if status == 202:
            time.sleep(10)
    finally:
        resolve_status, resolve_result = post(endpoint, resolution)
    if status == 202 and result and result.get("status") == "success" and result.get("dedup_key") == key:
        print("PagerDuty: evento sintetico aceito pela Events API.", flush=True)
    else:
        print("PagerDuty: trigger não confirmado; HTTP: " + str(status), flush=True)
        errors.append("pagerduty-trigger")
    if (resolve_status == 202 and resolve_result and resolve_result.get("status") == "success"
            and resolve_result.get("dedup_key") == key):
        print("PagerDuty: resolução do mesmo evento aceita pela Events API.", flush=True)
    else:
        print("PagerDuty: resolução não confirmada; HTTP: " + str(resolve_status), flush=True)
        errors.append("pagerduty-resolve")
if errors:
    raise SystemExit(1)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pagerduty-only", action="store_true")
    args = parser.parse_args()
    if not os.environ.get("KUBECONFIG"):
        raise RuntimeError("KUBECONFIG local obrigatório")
    server = subprocess.check_output(
        smoke.KUBECTL + ["config", "view", "--minify", "-o",
                         "jsonpath={.clusters[0].cluster.server}"], text=True
    )
    if not re.fullmatch(r"https://127\.0\.0\.1:\d+", server):
        raise RuntimeError("API Kubernetes não está no loopback")
    name = "external-notifications-" + uuid.uuid4().hex[:10]
    references = [
        ("PAGERDUTY_INTEGRATION_KEY", "notification-credentials", "pagerduty-key"),
    ]
    if not args.pagerduty_only:
        references.extend([
            ("GRAFANA_USER", "grafana-admin", "username"),
            ("GRAFANA_PASSWORD", "grafana-admin", "password"),
            ("TEAMS_WEBHOOK_URL", "notification-credentials", "teams-webhook"),
        ])
    env = [
        {"name": name, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}
        for name, secret, key in references
    ]
    env.append({"name": "TEST_TEAMS", "value": "0" if args.pagerduty_only else "1"})
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
                    raise RuntimeError("Uma ou mais notificações não foram confirmadas")
                return
            time.sleep(3)
        raise RuntimeError("Teste de notificações excedeu o prazo; verifique o incidente PagerDuty")
    finally:
        subprocess.run(smoke.KUBECTL + ["-n", "observability", "delete", "pod", name,
                                       "--ignore-not-found", "--wait=true"],
                       capture_output=True, check=True)


if __name__ == "__main__":
    main()
