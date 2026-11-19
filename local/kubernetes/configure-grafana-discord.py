#!/usr/bin/env python3
"""Provision native Grafana Discord delivery; an explicit flag enables one test."""
import argparse
import base64
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import smoke


POLICY_FILE = Path(__file__).with_name("self-healing-discord.yaml")
WEBHOOK_UID = "cg4m926zfjim9e"
GRAFANA = "http://127.0.0.1:13000"

PROBE = r'''
import base64, json, os, urllib.error, urllib.parse, urllib.request, uuid

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

_, health = request("/api/health")
if health.get("version") != "11.2.2":
    raise SystemExit("Versão Grafana inesperada.")
_, exported = request("/api/v1/provisioning/contact-points/export?name=Discord&decrypt=true&format=json")
contacts = [r for c in exported.get("contactPoints", []) for r in c.get("receivers", [])
            if r.get("uid") == "cg4m926zfjim9e" and r.get("type") == "discord"]
if len(contacts) != 1 or contacts[0].get("settings", {}).get("url") != os.environ["DISCORD_WEBHOOK_URL"]:
    raise SystemExit("Contact point Discord não persistiu com o Secret local.")
policy_status, policy = request("/api/v1/provisioning/policies")
routes = policy.get("routes", [])
expected = {("alertname", "=", "Auth com taxa elevada de erros 5xx"),
            ("service", "=", "auth-service"), ("namespace", "=", "toggle")}
local = [r for r in routes if r.get("receiver") == "local-self-healing"]
discord = [r for r in routes if r.get("receiver") == "Discord"]
def matchers(route):
    return {tuple(m) for m in route.get("object_matchers", [])}
if (policy_status != 200 or len(routes) != 2 or len(local) != 1 or len(discord) != 1
        or not local[0].get("continue") or local[0].get("routes")
        or discord[0].get("routes") or matchers(local[0]) != expected
        or matchers(discord[0]) != expected):
    raise SystemExit("Roteamento Discord não está restrito ao alerta auth esperado.")

if os.environ.get("SEND_TEST") != "true":
    print("Contact point e rota persistidos e verificados; nenhum teste enviado.", flush=True)
    raise SystemExit(0)

labels = {"alertname": "ToggleDiscordSyntheticTest", "instance": "synthetic-local",
          "test_run": uuid.uuid4().hex}
settings = dict(contacts[0].get("settings", {}))
settings.update({"title": "Teste sintetico ToggleMaster TC04-Discord",
                 "message": "Teste sintetico autorizado TC04-Discord; nenhum dado real."})
body = {"alert": {"labels": labels,
                  "annotations": {"summary": "Teste sintetico TC04-Discord"}},
        "receivers": [{"name": "Discord", "grafana_managed_receiver_configs": [
            {"uid": "cg4m926zfjim9e", "name": "Discord", "type": "discord",
             "settings": settings}]}]}
try:
    status, result = request("/api/alertmanager/grafana/config/api/v1/receivers/test", body)
except (urllib.error.URLError, TimeoutError, ValueError, OSError):
    raise SystemExit("Grafana não confirmou o teste sintético Discord.") from None
configs = [c for r in result.get("receivers", [])
           for c in r.get("grafana_managed_receiver_configs", [])]
if status != 200 or len(configs) != 1 or configs[0].get("status") != "ok":
    raise SystemExit("Grafana não confirmou o teste sintético Discord.")
print("Grafana confirmou o envio do teste sintético TC04-Discord.", flush=True)
print("A visualização da mensagem no canal não foi verificada independentemente.", flush=True)
'''


def kubectl(*args, input_text=None):
    return subprocess.run(
        smoke.KUBECTL + list(args), input=input_text, text=True, capture_output=True,
        check=True,
    ).stdout


def validate_webhook(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "discord.com"
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not re.fullmatch(r"/api/webhooks/[0-9]+/[^/]+", parsed.path)):
        raise ValueError("Webhook precisa ser HTTPS e apontar para discord.com.")


def apply_secret(webhook):
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "discord-webhook", "namespace": "observability"},
        "type": "Opaque",
        "data": {"url": base64.b64encode(webhook.encode()).decode()},
    }
    kubectl("apply", "--server-side", "--field-manager=toggle-discord-config",
            "-f", "-", input_text=json.dumps(secret))


def verify_existing_configuration():
    current = json.loads(kubectl(
        "-n", "observability", "get", "configmap/grafana-alerts", "-o", "json"
    ))
    existing = current.get("data", {}).get("self-healing-contactpoint.yaml")
    base = Path(__file__).with_name("self-healing-contactpoint.yaml").read_text()
    desired = POLICY_FILE.read_text()
    if existing not in (base, desired):
        raise RuntimeError("A política ativa diverge dos arquivos locais; atualização interrompida.")
    keys = kubectl(
        "-n", "observability", "get", "secret", "self-healing-client",
        "-o", 'go-template={{range $k,$v := .data}}{{if $v}}{{$k}}{{"\\n"}}{{end}}{{end}}',
    ).splitlines()
    if sorted(keys) != ["tls.crt", "token"]:
        raise RuntimeError("Secret do responder local incompleto; atualização interrompida.")


def verify_existing_contactpoint():
    secret = json.loads(kubectl(
        "-n", "observability", "get", "secret", "grafana-admin", "-o", "json"
    ))
    user = base64.b64decode(secret["data"]["username"])
    password = base64.b64decode(secret["data"]["password"])
    auth = base64.b64encode(user + b":" + password).decode()
    request = urllib.request.Request(
        GRAFANA + "/api/v1/provisioning/contact-points/export?"
        "name=Discord&decrypt=true&format=json",
        headers={"Authorization": "Basic " + auth},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            exported = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        raise RuntimeError("Contact point Discord atual não pôde ser verificado.") from None
    contacts = [receiver for point in exported.get("contactPoints", [])
                for receiver in point.get("receivers", [])
                if receiver.get("uid") == WEBHOOK_UID and receiver.get("type") == "discord"]
    if len(contacts) != 1:
        raise RuntimeError("Contact point Discord atual diverge; atualização interrompida.")


def patch_grafana():
    policy = POLICY_FILE.read_text()
    config_patch = {"data": {"self-healing-contactpoint.yaml": policy}}
    kubectl("-n", "observability", "patch", "configmap/grafana-alerts",
            "--type=merge", "-p", json.dumps(config_patch))
    deployment_patch = {
        "spec": {"template": {
            "metadata": {"annotations": {"toggle.local/discord-config": uuid.uuid4().hex}},
            "spec": {"containers": [{
                "name": "grafana",
                "env": [{
                    "name": "DISCORD_WEBHOOK_URL",
                    "valueFrom": {"secretKeyRef": {
                        "name": "discord-webhook", "key": "url", "optional": True,
                    }},
                }],
            }]},
        }},
    }
    kubectl("-n", "observability", "patch", "deployment/grafana",
            "--type=strategic", "-p", json.dumps(deployment_patch))
    kubectl("-n", "observability", "rollout", "status", "deployment/grafana",
            "--timeout=180s")


def run_verification(send_test):
    name = "grafana-discord-" + uuid.uuid4().hex[:10]
    env = [
        {"name": env_name, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}
        for env_name, secret, key in (
            ("USER_NAME", "grafana-admin", "username"),
            ("PASSWORD", "grafana-admin", "password"),
            ("DISCORD_WEBHOOK_URL", "discord-webhook", "url"),
        )
    ]
    pod = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "namespace": "observability"},
        "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                 "containers": [{
                     "name": "probe", "image": "python:3.11-slim",
                     "imagePullPolicy": "Never", "command": ["python3", "-u", "-c", PROBE],
                     "env": env + [{"name": "SEND_TEST",
                                    "value": "true" if send_test else "false"}],
                 }]},
    }
    kubectl("create", "-f", "-", input_text=json.dumps(pod))
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            state = json.loads(kubectl("-n", "observability", "get", "pod", name, "-o", "json"))
            phase = state["status"].get("phase")
            if phase in ("Succeeded", "Failed"):
                output = kubectl("-n", "observability", "logs", name).strip()
                if output:
                    print(output)
                if phase != "Succeeded":
                    raise RuntimeError("Teste Grafana/Discord não foi confirmado.")
                return
            time.sleep(3)
        raise RuntimeError("Teste Grafana/Discord excedeu o prazo.")
    finally:
        kubectl("-n", "observability", "delete", "pod", name,
                "--ignore-not-found", "--wait=true")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true",
                        help="verify persisted Secret, contact point, and policy without changing them")
    parser.add_argument("--send-test", action="store_true",
                        help="send exactly one synthetic Discord notification")
    args = parser.parse_args()
    if not os.environ.get("KUBECONFIG"):
        raise RuntimeError("KUBECONFIG local obrigatório.")
    if args.verify_only:
        if args.send_test:
            parser.error("--verify-only cannot be combined with --send-test")
        verify_existing_configuration()
        secret = json.loads(kubectl(
            "-n", "observability", "get", "secret", "discord-webhook", "-o", "json"
        ))
        if "url" not in secret.get("data", {}):
            raise RuntimeError("Secret Discord local incompleto.")
        run_verification(send_test=False)
        return
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    validate_webhook(webhook)
    server = kubectl("config", "view", "--minify", "-o",
                     "jsonpath={.clusters[0].cluster.server}")
    if not re.fullmatch(r"https://127\.0\.0\.1:\d+", server):
        raise RuntimeError("API Kubernetes não está no loopback.")
    verify_existing_configuration()
    verify_existing_contactpoint()
    try:
        with urllib.request.urlopen(GRAFANA + "/api/health", timeout=5) as response:
            if response.status != 200:
                raise RuntimeError("Grafana local indisponível.")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError("Grafana local indisponível.") from None
    try:
        apply_secret(webhook)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"Não foi possível aplicar o Secret local (kubectl rc={error.returncode}).") from None
    try:
        patch_grafana()
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"Não foi possível atualizar o Grafana local (kubectl rc={error.returncode}).") from None
    try:
        run_verification(args.send_test)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"Não foi possível concluir o teste Grafana/Discord (kubectl rc={error.returncode}).") from None


if __name__ == "__main__":
    main()
