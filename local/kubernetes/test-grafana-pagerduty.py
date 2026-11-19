#!/usr/bin/env python3
"""Test the provisioned PagerDuty contact point with an authorized synthetic event."""
import json
import os
import re
import subprocess
import time
import uuid

import smoke


PROBE = r'''
import base64, datetime, hashlib, json, os, time, urllib.error, urllib.request, uuid

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

client = urllib.request.build_opener(NoRedirect)

def fingerprint(labels):
    value = 14695981039346656037
    for key in sorted(labels):
        for byte in key.encode() + b"\xff" + labels[key].encode() + b"\xff":
            value = ((value ^ byte) * 1099511628211) & 0xffffffffffffffff
    return f"{value:016x}"

def dedup(receiver, labels, timestamp):
    group = f"{receiver}-{fingerprint(labels)}-{timestamp}"
    return hashlib.sha256(group.encode()).hexdigest()

auth = base64.b64encode((os.environ["USER_NAME"] + ":" + os.environ["PASSWORD"]).encode()).decode()
base = "http://grafana.observability.svc.cluster.local:3000"

def grafana(path, body=None):
    headers = {"Authorization": "Basic " + auth}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Request-Timeout"] = "5"
        data = json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, headers=headers)
    with client.open(request, timeout=15) as response:
        return response.status, json.load(response)

def resolve(key):
    body = {"routing_key": os.environ["PAGERDUTY_KEY"],
            "event_action": "resolve", "dedup_key": key}
    request = urllib.request.Request(
        "https://events.pagerduty.com/v2/enqueue", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with client.open(request, timeout=5) as response:
            result = json.load(response)
            return (response.status == 202 and result.get("status") == "success"
                    and result.get("dedup_key") == key)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return False

_, health = grafana("/api/health")
if health.get("version") != "11.2.2":
    raise SystemExit("Versão Grafana não verificada para o cálculo de deduplicação.")
_, exported = grafana("/api/v1/provisioning/contact-points/export?name=local-pagerduty&decrypt=true&format=json")
contacts = [r for c in exported.get("contactPoints", []) for r in c.get("receivers", [])
            if r.get("uid") == "local-pagerduty" and r.get("type") == "pagerduty"]
if len(contacts) != 1:
    raise SystemExit("Contact point PagerDuty provisionado não encontrado.")
settings = contacts[0].get("settings", {})
if settings.get("integrationKey") != os.environ["PAGERDUTY_KEY"]:
    raise SystemExit("Chave do contact point diverge do Secret local.")
settings.pop("integrationKey")
if settings.get("url", "https://events.pagerduty.com/v2/enqueue") != "https://events.pagerduty.com/v2/enqueue":
    raise SystemExit("Destino PagerDuty inesperado.")
settings.update({"source": "toggle-local-synthetic",
                 "client_url": "http://localhost:3000/",
                 "summary": "Teste sintetico ToggleMaster",
                 "severity": "info",
                 "details": {"firing": "synthetic", "resolved": "synthetic"}})
labels = {"alertname": "ToggleGrafanaSyntheticTest", "instance": "synthetic-local",
          "test_run": uuid.uuid4().hex}
body = {"alert": {"labels": labels, "annotations": {"summary": "Teste sintetico"}},
        "receivers": [{"name": "local-pagerduty", "grafana_managed_receiver_configs": [
            {"uid": "local-pagerduty", "name": "local-pagerduty",
             "type": "pagerduty", "settings": settings}]}]}
start = int(time.time())
status = result = None
try:
    status, result = grafana("/api/alertmanager/grafana/config/api/v1/receivers/test", body)
except (urllib.error.URLError, TimeoutError, ValueError, OSError):
    pass
end = int(time.time())
if result and result.get("notified_at") and result.get("alert", {}).get("labels"):
    timestamp = int(datetime.datetime.fromisoformat(
        result["notified_at"].replace("Z", "+00:00")).timestamp())
    keys = [dedup("local-pagerduty", result["alert"]["labels"], timestamp)]
else:
    # kind and this probe share the host clock; also cover adjacent seconds.
    keys = [dedup("local-pagerduty", labels, t) for t in range(start - 1, end + 2)]
if status == 200:
    configs = [c for r in result.get("receivers", [])
               for c in r.get("grafana_managed_receiver_configs", [])]
    sent = len(configs) == 1 and configs[0].get("status") == "ok"
else:
    sent = False
if sent:
    print("Grafana: contact point real aceitou o teste sintético PagerDuty.", flush=True)
else:
    print("Grafana: envio não confirmado; tentando resolução preventiva.", flush=True)
time.sleep(10)
resolved = [resolve(key) for key in keys]
if all(resolved):
    print("PagerDuty: resolução do evento de teste aceita pela Events API.", flush=True)
else:
    print("PagerDuty: resolução não confirmada; verificar incidente no PagerDuty.", flush=True)
if not sent or not all(resolved) or len(keys) != 1:
    raise SystemExit(1)
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
    name = "grafana-pagerduty-" + uuid.uuid4().hex[:10]
    env = [
        {"name": name, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}
        for name, secret, key in (
            ("USER_NAME", "grafana-admin", "username"),
            ("PASSWORD", "grafana-admin", "password"),
            ("PAGERDUTY_KEY", "notification-credentials", "pagerduty-key"),
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
                    raise RuntimeError("Teste Grafana/PagerDuty não foi confirmado")
                return
            time.sleep(3)
        raise RuntimeError("Teste excedeu prazo; verifique o incidente PagerDuty")
    finally:
        subprocess.run(smoke.KUBECTL + ["-n", "observability", "delete", "pod", name,
                                       "--ignore-not-found", "--wait=true"],
                       capture_output=True, check=True)


if __name__ == "__main__":
    main()
