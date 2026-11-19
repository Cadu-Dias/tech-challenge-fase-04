#!/usr/bin/env python3
"""Exercise Grafana -> TLS webhook -> auth rollout with synthetic OTLP metrics."""
import json
import os
import subprocess
import time
import uuid

import smoke


EMITTER = r'''
import json, ssl, time, urllib.parse, urllib.request
token = urllib.parse.unquote(open("/var/run/auth/headers").read().strip().split("=", 1)[1])
ssl_context = ssl.create_default_context(cafile="/var/run/tls/tls.crt")
start = time.time_ns()
count = 0
while True:
    count += 100
    metric = {"name":"http.server.request.duration","unit":"s","histogram":{
        "aggregationTemporality":2,"dataPoints":[{
            "attributes":[{"key":"http.response.status_code","value":{"intValue":"500"}},
                          {"key":"http.route","value":{"stringValue":"/synthetic-e2e"}}],
            "startTimeUnixNano":str(start),"timeUnixNano":str(time.time_ns()),
            "count":str(count),"sum":float(count)/10,"bucketCounts":[str(count)]}]}}
    payload = {"resourceMetrics":[{"resource":{"attributes":[
        {"key":"service.name","value":{"stringValue":"auth-service"}}]},
        "scopeMetrics":[{"scope":{"name":"synthetic-e2e"},"metrics":[metric]}]}]}
    request = urllib.request.Request(
        "https://otel-collector.observability.svc.cluster.local:4319/v1/metrics",
        data=json.dumps(payload).encode(),
        headers={"Authorization":token,"Content-Type":"application/json"})
    with urllib.request.urlopen(request, context=ssl_context, timeout=5) as response:
        if response.status != 200: raise RuntimeError("OTLP não aceito")
    print("synthetic metric sent", flush=True)
    time.sleep(10)
'''

CHECK_ALERT = r'''
import base64, json, os, urllib.request
token = base64.b64encode((os.environ["USER_NAME"] + ":" + os.environ["PASSWORD"]).encode()).decode()
request = urllib.request.Request("http://grafana.observability.svc.cluster.local:3000/api/alertmanager/grafana/api/v2/alerts",
                                 headers={"Authorization":"Basic " + token})
with urllib.request.urlopen(request, timeout=5) as response: alerts = json.load(response)
active = any(a.get("labels", {}).get("alertname") == "Auth com taxa elevada de erros 5xx"
             and a.get("status", {}).get("state") == "active" for a in alerts)
print("active" if active else "inactive")
'''


def kubectl(*args, **kwargs):
    return subprocess.run(smoke.KUBECTL + list(args), check=True, text=True, **kwargs)


def create_pod(namespace, name, container):
    pod = {"apiVersion": "v1", "kind": "Pod",
           "metadata": {"name": name, "namespace": namespace},
           "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                    "containers": [container]}}
    if namespace == "toggle":
        pod["spec"]["volumes"] = [
            {"name": "auth", "secret": {"secretName": "auth-metrics-client"}},
            {"name": "tls", "secret": {"secretName": "telemetry-auth",
                                        "items": [{"key": "tls.crt", "path": "tls.crt"}]}}]
    kubectl("create", "-f", "-", input=json.dumps(pod), capture_output=True)


def state(probe):
    result = kubectl("-n", "observability", "exec", probe, "--", "python3", "-c",
                     CHECK_ALERT, capture_output=True)
    return result.stdout.strip()


def restart_marker():
    result = kubectl("-n", "toggle", "get", "deployment", "auth-service", "-o",
                     'go-template={{index .spec.template.metadata.annotations "togglemaster.io/restarted-at"}}',
                     capture_output=True)
    return result.stdout.strip()


def restart_count():
    result = kubectl("-n", "toggle", "logs", "deployment/toggle-self-healing", capture_output=True)
    count = 0
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "self_healing.result" and "restarted" in event.get("results", []):
            count += 1
    return count


def main():
    if not os.environ.get("KUBECONFIG"):
        raise RuntimeError("KUBECONFIG local obrigatório")
    server = kubectl("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}",
                     capture_output=True).stdout
    if not server.startswith("https://127.0.0.1:"):
        raise RuntimeError("API Kubernetes não está no loopback")
    suffix = uuid.uuid4().hex[:10]
    probe, emitter = "grafana-probe-" + suffix, "auth-metrics-e2e-" + suffix
    created = []
    try:
        create_pod("observability", probe, {
            "name": "probe", "image": "python:3.11-slim", "imagePullPolicy": "Never",
            "command": ["python3", "-c", "import time; time.sleep(900)"],
            "env": [
                {"name": "USER_NAME", "valueFrom": {"secretKeyRef": {"name": "grafana-admin", "key": "username"}}},
                {"name": "PASSWORD", "valueFrom": {"secretKeyRef": {"name": "grafana-admin", "key": "password"}}}]})
        created.append(("observability", probe))
        kubectl("-n", "observability", "wait", "--for=condition=Ready", "pod/" + probe, "--timeout=90s",
                capture_output=True)
        if state(probe) != "inactive":
            raise RuntimeError("alerta auth já estava ativo; teste não iniciado")
        before = restart_marker()
        previous_restarts = restart_count()
        create_pod("toggle", emitter, {
            "name": "emitter", "image": "python:3.11-slim", "imagePullPolicy": "Never",
            "command": ["python3", "-u", "-c", EMITTER],
            "volumeMounts": [{"name": "auth", "mountPath": "/var/run/auth", "readOnly": True},
                             {"name": "tls", "mountPath": "/var/run/tls", "readOnly": True}]})
        created.append(("toggle", emitter))
        deadline = time.monotonic() + 300
        fired = False
        while time.monotonic() < deadline:
            if state(probe) == "active":
                fired = True
            if fired and restart_marker() != before and restart_count() > previous_restarts:
                break
            time.sleep(5)
        else:
            raise RuntimeError("alerta ou rollout não ocorreram dentro do prazo")
        kubectl("-n", "toggle", "rollout", "status", "deployment/auth-service", "--timeout=150s",
                capture_output=True)
        kubectl("-n", "toggle", "delete", "pod", emitter, "--wait=true", capture_output=True)
        created.remove(("toggle", emitter))
        deadline = time.monotonic() + 360
        while time.monotonic() < deadline:
            if state(probe) == "inactive":
                print("Grafana alertou, webhook TLS acionou rollout auth e alerta foi resolvido.")
                return
            time.sleep(10)
        raise RuntimeError("alerta sintético não foi resolvido após interromper emissor")
    finally:
        for namespace, name in reversed(created):
            kubectl("-n", namespace, "delete", "pod", name, "--wait=true", "--ignore-not-found",
                    capture_output=True)


if __name__ == "__main__":
    main()
