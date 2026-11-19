#!/usr/bin/env python3
"""Configure and optionally test a native Telegram contact point in local Grafana."""
import argparse
import base64
import getpass
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import uuid
from pathlib import Path

import smoke


NAMESPACE = "observability"
SECRET_NAME = "telegram-alerts"
CONTACT_NAME = "Telegram"
CONTACT_UID = "tc04telegram"
CONFIG_KEY = "telegram-contactpoint.json"
LEGACY_CONFIG_KEY = "telegram-contactpoint.yaml"
CONFIG_FILE = Path(__file__).with_name(CONFIG_KEY)
GRAFANA_SERVICE = "http://grafana.observability.svc.cluster.local:3000"

PROBE = r'''
import base64, json, os, time, urllib.error, urllib.parse, urllib.request, uuid

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

deadline = time.monotonic() + 60
while True:
    try:
        _, health = request("/api/health")
        break
    except urllib.error.URLError:
        if time.monotonic() >= deadline:
            raise SystemExit("Grafana não respondeu ao health check interno.")
        time.sleep(2)
if health.get("version") != "11.2.2":
    raise SystemExit("Versão Grafana inesperada.")

query = urllib.parse.urlencode({"name": "Telegram", "decrypt": "true", "format": "json"})
_, exported = request("/api/v1/provisioning/contact-points/export?" + query)
contacts = [receiver for point in exported.get("contactPoints", [])
            for receiver in point.get("receivers", [])
            if receiver.get("uid") == "tc04telegram" and receiver.get("type") == "telegram"]
if len(contacts) != 1:
    raise SystemExit("Contact point Telegram não foi provisionado.")
settings = contacts[0].get("settings", {})
if (settings.get("bottoken") != os.environ["TELEGRAM_BOT_TOKEN"]
        or str(settings.get("chatid")) != os.environ["TELEGRAM_CHAT_ID"]):
    raise SystemExit("Contact point Telegram diverge do Secret local.")

_, policy = request("/api/v1/provisioning/policies")
allowed = [
    {("alertname", "=", "Auth com taxa elevada de erros 5xx"),
     ("namespace", "=", "toggle"), ("service", "=", "auth-service")},
    {("alertname", "=", "Taxa elevada de erros 5xx nos serviços HTTP")},
    {("alertname", "=", "Latência elevada nos serviços HTTP")},
]
def check_routes(route):
    if route.get("receiver") == "Telegram":
        matchers = {tuple(m) for m in route.get("object_matchers", [])}
        if matchers not in allowed or route.get("routes"):
            raise SystemExit("Rota Telegram fora do escopo auth/HTTP esperado.")
    for child in route.get("routes", []):
        check_routes(child)
check_routes(policy)

if os.environ.get("SEND_TEST") != "true":
    print("Contact point Telegram verificado; nenhuma mensagem enviada.", flush=True)
    raise SystemExit(0)

labels = {"alertname": "ToggleTelegramSyntheticTest", "instance": "synthetic-local",
          "test_run": uuid.uuid4().hex}
body = {
    "alert": {"labels": labels,
              "annotations": {"summary": "Teste sintetico ToggleMaster Telegram"}},
    "receivers": [{"name": "Telegram", "grafana_managed_receiver_configs": [{
        "uid": "tc04telegram", "name": "Telegram", "type": "telegram",
        "settings": settings,
    }]}],
}
try:
    status, result = request("/api/alertmanager/grafana/config/api/v1/receivers/test", body)
except urllib.error.HTTPError as error:
    raise SystemExit("Grafana recusou o teste Telegram (HTTP %s)." % error.code) from None
except (urllib.error.URLError, TimeoutError, ValueError, OSError):
    raise SystemExit("Grafana não confirmou o teste Telegram.") from None
configs = [config for receiver in result.get("receivers", [])
           for config in receiver.get("grafana_managed_receiver_configs", [])]
if status != 200 or len(configs) != 1 or configs[0].get("status") != "ok":
    raise SystemExit("Grafana não confirmou sucesso do receiver Telegram.")
print("Grafana confirmou o envio do teste sintético Telegram.", flush=True)
print("A visualização da mensagem no chat não foi verificada independentemente.", flush=True)
'''


def kubectl(*args, input_text=None):
    return subprocess.run(
        smoke.KUBECTL + list(args), input=input_text, text=True, capture_output=True,
        check=True,
    ).stdout


def validate_values(token, chat_id):
    if not token or any(char.isspace() for char in token):
        raise ValueError("Token Telegram vazio ou inválido.")
    if not re.fullmatch(r"-?\d+|@[A-Za-z0-9_]{5,32}", chat_id):
        raise ValueError("Chat ID deve ser numérico ou @username de canal.")


def apply_secret(token, chat_id):
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": SECRET_NAME, "namespace": NAMESPACE},
        "type": "Opaque",
        "data": {
            "bot-token": base64.b64encode(token.encode()).decode(),
            "chat-id": base64.b64encode(chat_id.encode()).decode(),
        },
    }
    subprocess.run(
        smoke.KUBECTL + ["-n", NAMESPACE, "apply", "--server-side",
                         "--field-manager=toggle-grafana-telegram", "-f", "-"],
        input=json.dumps(secret), text=True, capture_output=True, check=True,
    )


def verify_local_cluster():
    if not os.environ.get("KUBECONFIG"):
        raise RuntimeError("KUBECONFIG local obrigatório.")
    server = kubectl(
        "config", "view", "--minify", "-o",
        "jsonpath={.clusters[0].cluster.server}",
    )
    if not re.fullmatch(r"https://127\.0\.0\.1:\d+", server):
        raise RuntimeError("Contexto Kubernetes não aponta para o cluster local.")
    ready = kubectl(
        "-n", NAMESPACE, "get", "deployment/grafana", "-o",
        "jsonpath={.status.readyReplicas}/{.spec.replicas}",
    )
    if ready != "1/1":
        raise RuntimeError("Grafana não está pronto.")


def verify_secret_keys():
    keys = kubectl(
        "-n", NAMESPACE, "get", "secret", SECRET_NAME,
        "-o", 'go-template={{range $k,$v := .data}}{{if $v}}{{$k}}{{"\\n"}}{{end}}{{end}}',
    ).splitlines()
    if sorted(keys) != ["bot-token", "chat-id"]:
        raise RuntimeError("Secret Telegram local ausente ou incompleto.")


def render_contactpoint():
    secret = json.loads(kubectl(
        "-n", NAMESPACE, "get", "secret", SECRET_NAME, "-o", "json",
    ))
    chat_id = base64.b64decode(secret["data"]["chat-id"]).decode()
    document = json.loads(CONFIG_FILE.read_text())
    document["contactPoints"][0]["receivers"][0]["settings"]["chatid"] = chat_id
    return json.dumps(document, indent=2) + "\n"


def is_managed_contactpoint(value):
    try:
        document = json.loads(value)
        points = document.get("contactPoints", [])
        receiver = points[0]["receivers"][0]
        settings = receiver["settings"]
    except (ValueError, KeyError, IndexError, TypeError):
        return False
    return (
        document.get("apiVersion") == 1
        and len(points) == 1
        and points[0].get("name") == CONTACT_NAME
        and receiver.get("uid") == CONTACT_UID
        and receiver.get("type") == "telegram"
        and settings.get("bottoken") == "$TELEGRAM_BOT_TOKEN"
        and set(settings) == {"bottoken", "chatid"}
    )


def patch_contactpoint():
    current = json.loads(kubectl(
        "-n", NAMESPACE, "get", "configmap/grafana-alerts", "-o", "json",
    ))
    data = current.get("data", {})
    existing = data.get(CONFIG_KEY)
    legacy = data.get(LEGACY_CONFIG_KEY)
    desired = render_contactpoint()
    legacy_prefix = (
        "apiVersion: 1\ncontactPoints:\n  - orgId: 1\n"
        "    name: Telegram\n    receivers:\n      - uid: tc04telegram\n"
        "        type: telegram\n        settings:\n"
        "          bottoken: $TELEGRAM_BOT_TOKEN\n"
    )
    known_legacy = {
        legacy_prefix + "          chatid: $TELEGRAM_CHAT_ID\n",
        legacy_prefix + '          chatid: "$TELEGRAM_CHAT_ID"\n',
        legacy_prefix + "          chatid: !!str $TELEGRAM_CHAT_ID\n",
    }
    if existing is not None and not is_managed_contactpoint(existing):
        raise RuntimeError("Configuração Telegram ativa diverge do arquivo local.")
    if legacy is not None and legacy not in known_legacy:
        raise RuntimeError("Arquivo Telegram legado diverge; atualização interrompida.")
    patch_data = {CONFIG_KEY: desired}
    if legacy is not None:
        patch_data[LEGACY_CONFIG_KEY] = None
    kubectl(
        "-n", NAMESPACE, "patch", "configmap/grafana-alerts",
        "--type=merge", "-p", json.dumps({"data": patch_data}),
    )


def restart_grafana():
    patch = {
        "spec": {"template": {
            "metadata": {"annotations": {"toggle.local/telegram-config": uuid.uuid4().hex}},
            "spec": {"containers": [{
                "name": "grafana",
                "env": [
                    {"name": "TELEGRAM_BOT_TOKEN", "valueFrom": {"secretKeyRef": {
                        "name": SECRET_NAME, "key": "bot-token",
                    }}},
                    {"name": "TELEGRAM_CHAT_ID", "valueFrom": {"secretKeyRef": {
                        "name": SECRET_NAME, "key": "chat-id",
                    }}},
                ],
            }]},
        }},
    }
    kubectl(
        "-n", NAMESPACE, "patch", "deployment/grafana",
        "--type=strategic", "-p", json.dumps(patch),
    )
    kubectl(
        "-n", NAMESPACE, "rollout", "status", "deployment/grafana",
        "--timeout=180s",
    )


def run_probe(send_test):
    name = "grafana-telegram-" + uuid.uuid4().hex[:10]
    env = [
        {"name": env_name, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}
        for env_name, secret, key in (
            ("USER_NAME", "grafana-admin", "username"),
            ("PASSWORD", "grafana-admin", "password"),
            ("TELEGRAM_BOT_TOKEN", SECRET_NAME, "bot-token"),
            ("TELEGRAM_CHAT_ID", SECRET_NAME, "chat-id"),
        )
    ]
    pod = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "namespace": NAMESPACE},
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
            state = json.loads(kubectl("-n", NAMESPACE, "get", "pod", name, "-o", "json"))
            phase = state["status"].get("phase")
            if phase in ("Succeeded", "Failed"):
                output = kubectl("-n", NAMESPACE, "logs", name).strip()
                if output:
                    print(output)
                if phase != "Succeeded":
                    raise RuntimeError("Teste Grafana/Telegram não foi confirmado.")
                return
            time.sleep(3)
        raise RuntimeError("Teste Grafana/Telegram excedeu o prazo.")
    finally:
        kubectl("-n", NAMESPACE, "delete", "pod", name,
                "--ignore-not-found", "--wait=true")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true",
                        help="verify the persisted Telegram contact point without sending")
    parser.add_argument("--send-test", action="store_true",
                        help="send exactly one synthetic Telegram notification")
    args = parser.parse_args()
    if args.verify_only and args.send_test:
        parser.error("--verify-only cannot be combined with --send-test")
    verify_local_cluster()
    if args.verify_only:
        verify_secret_keys()
        run_probe(send_test=False)
        return
    if not sys.stdin.isatty():
        raise RuntimeError("Execute a configuração em um terminal interativo para ocultar as entradas.")
    token = getpass.getpass("Telegram Bot API token (input hidden): ")
    chat_id = getpass.getpass("Telegram chat ID (input hidden): ").strip()
    validate_values(token, chat_id)
    apply_secret(token, chat_id)
    patch_contactpoint()
    restart_grafana()
    run_probe(send_test=args.send_test)


if __name__ == "__main__":
    main()
