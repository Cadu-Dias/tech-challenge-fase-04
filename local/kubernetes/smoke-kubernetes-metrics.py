#!/usr/bin/env python3
"""Verify local Kubernetes dashboard queries through Grafana's actual datasource."""
import json
import os
import subprocess
import time
import uuid

import smoke


PROBE = r'''
import base64, json, os, time, urllib.parse, urllib.request
auth = base64.b64encode((os.environ["USER_NAME"] + ":" + os.environ["PASSWORD"]).encode()).decode()
base = "http://grafana.observability.svc.cluster.local:3000"
def get(path):
    request = urllib.request.Request(base + path, headers={"Authorization":"Basic " + auth})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)
dashboard = get("/api/dashboards/uid/toggle-overview")["dashboard"]
panels = {p["id"]:p for p in dashboard["panels"] if p.get("id") in (4,5,6,7)}
if len(panels) != 4: raise RuntimeError("Painéis Kubernetes ausentes")
deadline = time.monotonic() + 180
while time.monotonic() < deadline:
    counts = {}
    for pid, panel in panels.items():
        expr = panel["targets"][0]["expr"].replace("$namespace", "toggle").replace("$service", ".+")
        query = urllib.parse.urlencode({"query":expr})
        result = get("/api/datasources/proxy/uid/prometheus/api/v1/query?" + query)
        values = result["data"]["result"]
        if any(v["metric"].get("namespace") != "toggle" for v in values):
            raise RuntimeError("Painel retornou namespace inesperado")
        counts[str(pid)] = len(values)
    if all(counts.values()):
        print(json.dumps({"grafana_dashboard_panel_series": counts}), flush=True)
        break
    time.sleep(5)
else: raise RuntimeError("Grafana não recebeu séries dos quatro painéis")
'''


def main():
    if not os.environ.get("KUBECONFIG"):
        raise RuntimeError("KUBECONFIG local obrigatório")
    server = subprocess.check_output(
        smoke.KUBECTL + ["config", "view", "--minify", "-o",
                         "jsonpath={.clusters[0].cluster.server}"], text=True
    )
    if not server.startswith("https://127.0.0.1:"):
        raise RuntimeError("API Kubernetes não está no loopback")
    for namespace, account, verb, resource, expected in (
        ("toggle", "prometheus-local", "get", "nodes/toggle-observability-control-plane", "yes"),
        ("toggle", "prometheus-local", "get", "nodes/toggle-observability-control-plane", "no"),
        ("toggle", "kube-state-metrics-local", "list", "pods", "yes"),
        ("toggle", "kube-state-metrics-local", "list", "secrets", "no"),
        ("observability", "kube-state-metrics-local", "list", "pods", "no"),
    ):
        args = smoke.KUBECTL + ["-n", namespace, "auth", "can-i", verb, resource]
        if account == "prometheus-local":
            args += ["--subresource", "metrics" if expected == "yes" else "proxy"]
        args += ["--as", "system:serviceaccount:toggle:" + account]
        result = subprocess.run(args, capture_output=True, text=True)
        if result.stdout.strip() != expected or result.returncode not in (0, 1):
            raise RuntimeError("RBAC inesperado para " + account + " / " + resource)
    name = "grafana-metrics-probe-" + uuid.uuid4().hex[:10]
    container = {
        "name": "probe", "image": "python:3.11-slim", "imagePullPolicy": "Never",
        "command": ["python3", "-u", "-c", PROBE],
        "env": [
            {"name": "USER_NAME", "valueFrom": {"secretKeyRef": {"name": "grafana-admin", "key": "username"}}},
            {"name": "PASSWORD", "valueFrom": {"secretKeyRef": {"name": "grafana-admin", "key": "password"}}},
        ]}
    pod = {"apiVersion": "v1", "kind": "Pod",
           "metadata": {"name": name, "namespace": "observability"},
           "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                    "containers": [container]}}
    subprocess.run(smoke.KUBECTL + ["create", "-f", "-"], input=json.dumps(pod),
                   text=True, capture_output=True, check=True)
    try:
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            pod_json = json.loads(subprocess.check_output(
                smoke.KUBECTL + ["-n", "observability", "get", "pod", name, "-o", "json"]
            ))
            phase = pod_json["status"].get("phase")
            if phase in ("Succeeded", "Failed"):
                logs = subprocess.check_output(
                    smoke.KUBECTL + ["-n", "observability", "logs", name], text=True)
                if phase != "Succeeded":
                    raise RuntimeError("Teste Grafana falhou: " + logs)
                print(logs.strip())
                print("RBAC limitado ao nó e aos pods/deployments de toggle.")
                return
            time.sleep(3)
        raise RuntimeError("Teste Grafana excedeu prazo")
    finally:
        subprocess.run(smoke.KUBECTL + ["-n", "observability", "delete", "pod", name,
                                       "--ignore-not-found", "--wait=true"],
                       capture_output=True, check=True)


if __name__ == "__main__":
    main()
