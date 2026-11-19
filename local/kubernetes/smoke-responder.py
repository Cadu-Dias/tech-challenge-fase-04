#!/usr/bin/env python3
"""Exercise a single synthetic incident against the local responder."""
import json
import os
import ssl
import subprocess
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

import smoke


def send(base, token, body, context):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + "/webhook", data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, context=context, timeout=160) as response:
        return json.load(response)["results"]


def main():
    if not os.environ.get("KUBECONFIG"):
        raise RuntimeError("KUBECONFIG local obrigatório")
    server = subprocess.check_output(
        smoke.KUBECTL + ["config", "view", "--minify", "-o",
                         "jsonpath={.clusters[0].cluster.server}"], text=True
    )
    if not server.startswith("https://127.0.0.1:"):
        raise RuntimeError("API Kubernetes não está no loopback")
    service_account = "system:serviceaccount:toggle:toggle-self-healing"
    for action, resource, expected in (
        ("patch", "deployment/auth-service", "yes"),
        ("patch", "deployment/flag-service", "no"),
        ("update", "configmap/toggle-self-healing-state", "yes"),
        ("update", "configmap/auth-config", "no"),
    ):
        result = subprocess.run(
            smoke.KUBECTL + ["-n", "toggle", "auth", "can-i", action, resource,
                             "--as", service_account], capture_output=True, text=True
        )
        if result.stdout.strip() != expected or result.returncode not in (0, 1):
            raise RuntimeError("RBAC inesperado: " + action + " " + resource)
    with open(os.environ["TOKEN_FILE"], encoding="utf-8") as file:
        token = file.read().strip()
    context = ssl.create_default_context(cafile=os.environ["TLS_CERT_FILE"])
    base = forward_https()
    fingerprint = uuid.uuid4().hex
    alert = {
        "status": "firing", "fingerprint": fingerprint,
        "startsAt": datetime.now(timezone.utc).isoformat(),
        "labels": {"alertname": "Auth com taxa elevada de erros 5xx",
                   "service": "auth-service", "namespace": "toggle"},
    }
    try:
        send(base, None, {"alerts": [alert]}, context)
        raise RuntimeError("webhook aceitou chamada sem token")
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise
    if send(base, token, {"alerts": [alert]}, context) != ["restarted"]:
        raise RuntimeError("rollout não foi confirmado")
    if send(base, token, {"alerts": [alert]}, context) != ["duplicate"]:
        raise RuntimeError("deduplicação falhou")
    alert["fingerprint"] = uuid.uuid4().hex
    if send(base, token, {"alerts": [alert]}, context) != ["cooldown"]:
        raise RuntimeError("cooldown falhou")
    print(json.dumps({"tls_and_token": True, "restricted_rbac": True,
                      "rollout": True, "duplicate": True, "cooldown": True}))


def forward_https():
    return smoke.forward("toggle-self-healing", 8080).replace("http://", "https://", 1)


if __name__ == "__main__":
    try:
        main()
    finally:
        for process in smoke.PROCESSES:
            process.terminate()
        for process in smoke.PROCESSES:
            process.wait(timeout=10)
