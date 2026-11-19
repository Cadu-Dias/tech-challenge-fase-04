#!/usr/bin/env python3
"""Add scoped PagerDuty/Telegram routes and hot-reload local Grafana without restart."""
import base64
import copy
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.request

import smoke


KEY = "auth-http-notifications.json"
LEGACY_KEY = "self-healing-contactpoint.yaml"
RECEIVERS = {"local-pagerduty": "pagerduty", "Telegram": "telegram"}
RULES = {
    "togglemaster-auth-5xx": "Auth com taxa elevada de erros 5xx",
    "togglemaster-http-5xx": "Taxa elevada de erros 5xx nos serviços HTTP",
    "togglemaster-http-latency": "Latência elevada nos serviços HTTP",
}


def build_policy(current):
    policy = copy.deepcopy(current)
    policy.pop("provenance", None)
    policy["orgId"] = 1
    additions = []
    for uid, title in RULES.items():
        matchers = [["alertname", "=", title]]
        if uid == "togglemaster-auth-5xx":
            matchers += [["namespace", "=", "toggle"], ["service", "=", "auth-service"]]
        for receiver in RECEIVERS:
            additions.append({
                "receiver": receiver, "object_matchers": matchers,
                "continue": True, "group_wait": "0s",
                "group_interval": "30s", "repeat_interval": "4h",
            })
    existing = policy.get("routes", [])
    policy["routes"] = additions + [r for r in existing if r not in additions]
    return policy


def canonical(policy):
    result = copy.deepcopy(policy)
    result.pop("provenance", None)
    result.pop("orgId", None)
    if "object_matchers" in result:
        result["object_matchers"] = sorted(result["object_matchers"])
    if "routes" in result:
        result["routes"] = [canonical(r) for r in result["routes"]]
    return result


def kubectl(*args, input_text=None):
    return subprocess.check_output(
        smoke.KUBECTL + list(args), input=input_text,
        text=True, stderr=subprocess.PIPE,
    )


def main():
    if not os.environ.get("KUBECONFIG"):
        raise RuntimeError("KUBECONFIG local obrigatório.")
    server = kubectl("config", "view", "--minify", "-o",
                     "jsonpath={.clusters[0].cluster.server}")
    if not re.fullmatch(r"https://127\.0\.0\.1:\d+", server):
        raise RuntimeError("Cluster fora do loopback; operação recusada.")
    data = json.loads(kubectl(
        "-n", "observability", "get", "secret", "grafana-admin", "-o", "json",
    ))["data"]
    auth = "Basic " + base64.b64encode(
        base64.b64decode(data["username"]) + b":" + base64.b64decode(data["password"])
    ).decode()
    base = smoke.forward("grafana", 3000, namespace="observability")
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, reload=False):
        req = urllib.request.Request(
            base + path, headers={"Authorization": auth},
            data=b"" if reload else None,
        )
        with client.open(req, timeout=30) as response:
            return json.load(response)

    contacts = request("/api/v1/provisioning/contact-points")
    for name, kind in RECEIVERS.items():
        if not any(p.get("name") == name and p.get("type") == kind for p in contacts):
            raise RuntimeError("Contact point obrigatório ausente: " + name)
    rules = {r["uid"]: r for r in request("/api/v1/provisioning/alert-rules")}
    for uid, title in RULES.items():
        rule = rules.get(uid)
        if (not rule or rule["title"] != title or
                (rule.get("notification_settings") or {}).get("receiver")):
            raise RuntimeError("Regra ausente ou com envio direto; operação recusada: " + title)
    current = request("/api/v1/provisioning/policies")
    policy = build_policy(current)
    configmap = json.loads(kubectl(
        "-n", "observability", "get", "configmap", "grafana-alerts", "-o", "json",
    ))
    files = configmap["data"]
    legacy = subprocess.check_output(
        ["ruby", "-ryaml", "-rjson", "-e",
         "puts JSON.generate(YAML.safe_load(STDIN.read))"],
        input=files[LEGACY_KEY], text=True, stderr=subprocess.PIPE,
    )
    document = json.loads(legacy)
    document.pop("policies", None)
    legacy_content = json.dumps(document, indent=2) + "\n"
    content = json.dumps({"apiVersion": 1, "policies": [policy]}, indent=2) + "\n"
    # Refuse concurrent edits before persisting both provisioning files atomically.
    patch = [
        {"op": "test", "path": "/metadata/resourceVersion",
         "value": configmap["metadata"]["resourceVersion"]},
        {"op": "add", "path": "/data/" + KEY, "value": content},
        {"op": "replace", "path": "/data/" + LEGACY_KEY, "value": legacy_content},
    ]
    kubectl("-n", "observability", "patch", "configmap", "grafana-alerts",
            "--type=json", "--patch-file=/dev/stdin", input_text=json.dumps(patch))
    for filename, value in ((KEY, content), (LEGACY_KEY, legacy_content)):
        expected = hashlib.sha256(value.encode()).hexdigest()
        deadline = time.monotonic() + 180
        while True:
            checksum = kubectl(
                "-n", "observability", "exec", "deployment/grafana", "--",
                "sh", "-c", 'if test -f "$1"; then sha256sum "$1"; fi',
                "sh", "/etc/grafana/provisioning/alerting/" + filename,
            ).split()
            if checksum and checksum[0] == expected:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("ConfigMap salvo, mas projeção pendente; reload não executado.")
            time.sleep(3)
    request("/api/admin/provisioning/alerting/reload", reload=True)
    actual = request("/api/v1/provisioning/policies")
    if canonical(actual) != canonical(policy):
        raise RuntimeError("Política salva, mas configuração efetiva diverge; revise Grafana.")
    if request("/api/health").get("database") != "ok":
        raise RuntimeError("Grafana não confirmou saúde após reload.")
    print("Auth 5xx, HTTP 5xx e latência HTTP → PagerDuty + Telegram.")
    print("Rotas anteriores preservadas; hot reload concluído, sem restart ou teste sintético.")


if __name__ == "__main__":
    try:
        main()
    finally:
        for process in smoke.PROCESSES:
            process.terminate()
        for process in smoke.PROCESSES:
            process.wait(timeout=10)
