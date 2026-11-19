#!/usr/bin/env python3
"""Exercise a real, temporary auth fault and its local Grafana responder."""
import base64
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

import smoke


ALERT = "Auth com taxa elevada de erros 5xx"
EXPECTED_MATCHERS = {
    ("alertname", "=", ALERT),
    ("namespace", "=", "toggle"),
    ("service", "=", "auth-service"),
}


def validate_notification_policy(policy, allow_discord):
    routes = policy.get("routes", [])
    if policy.get("receiver") != "grafana-default-email":
        raise RuntimeError("receiver raiz inesperado")
    if len(routes) == 1:
        route = routes[0]
        matchers = {tuple(matcher) for matcher in route.get("object_matchers", [])}
        if (route.get("receiver") != "local-self-healing" or route.get("continue")
                or route.get("routes") or matchers != EXPECTED_MATCHERS):
            raise RuntimeError("roteamento ativo não garante isolamento de notificações")
        return
    local = [route for route in routes if route.get("receiver") == "local-self-healing"]
    discord = [route for route in routes if route.get("receiver") == "Discord"]
    if (len(routes) != 2 or len(local) != 1 or len(discord) != 1
            or not local[0].get("continue") or local[0].get("routes")
            or discord[0].get("routes")
            or {tuple(m) for m in local[0].get("object_matchers", [])} != EXPECTED_MATCHERS
            or {tuple(m) for m in discord[0].get("object_matchers", [])} != EXPECTED_MATCHERS):
        raise RuntimeError("roteamento ativo não garante isolamento de notificações")
    if not allow_discord:
        raise RuntimeError(
            "falha não executada: a política enviará o alerta auth ao Discord; "
            "autorize esta execução com ALLOW_DISCORD_FAULT_ALERTS=true"
        )


def kubectl(*args):
    return subprocess.check_output(smoke.KUBECTL + list(args), text=True)


def marker():
    return kubectl("-n", "toggle", "get", "deployment", "auth-service", "-o",
                   'go-template={{index .spec.template.metadata.annotations "togglemaster.io/restarted-at"}}').strip()


def status(base, path="/validate", data=None, headers=None):
    try:
        request = urllib.request.Request(base + path, data=data, headers=headers or {})
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def main():
    if not os.environ.get("KUBECONFIG"):
        raise RuntimeError("KUBECONFIG local obrigatório")
    server = kubectl("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}")
    if not re.fullmatch(r"https://127\.0\.0\.1:\d+", server):
        raise RuntimeError("API Kubernetes não está no loopback")
    deployment = json.loads(kubectl("-n", "toggle", "get", "deployment", "auth-service", "-o", "json"))
    env = deployment["spec"]["template"]["spec"]["containers"][0].get("env", [])
    if any(item["name"] == "AUTH_FAULT_INJECTION_ENABLED" for item in env):
        raise RuntimeError("configuração de falha já existe; não sobrescrever")
    state = json.loads(json.loads(kubectl("-n", "toggle", "get", "configmap",
                                       "toggle-self-healing-state", "-o", "json"))["data"]["state"])
    if "reserved" in state.get("incidents", {}).values() or time.time() - state.get("last_restart", 0) < 600:
        raise RuntimeError("responder ocupado ou em cooldown")

    secret = json.loads(kubectl("-n", "observability", "get", "secret", "grafana-admin", "-o", "json"))
    user = base64.b64decode(secret["data"]["username"])
    password = base64.b64decode(secret["data"]["password"])
    auth = "Basic " + base64.b64encode(user + b":" + password).decode()
    auth_secret = json.loads(kubectl("-n", "toggle", "get", "secret", "auth-secret", "-o", "json"))
    master_key = base64.b64decode(auth_secret["data"]["MASTER_KEY"]).decode()
    invalid_key = json.dumps({"name": "synthetic-" + "x" * 120}).encode()
    admin_headers = {"Authorization": "Bearer " + master_key, "Content-Type": "application/json"}
    grafana = smoke.forward("grafana", 3000, namespace="observability")

    def grafana_get(path):
        req = urllib.request.Request(grafana + path, headers={"Authorization": auth})
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)

    policy = grafana_get("/api/v1/provisioning/policies")
    validate_notification_policy(
        policy, allow_discord=os.environ.get("ALLOW_DISCORD_FAULT_ALERTS") == "true"
    )

    def alert_active():
        alerts = grafana_get("/api/alertmanager/grafana/api/v2/alerts")
        return any(a.get("labels", {}).get("alertname") == ALERT
                   and a.get("status", {}).get("state") == "active" for a in alerts)

    if alert_active():
        raise RuntimeError("alerta já estava ativo")
    before = marker()
    enabled = False
    fired = False
    restarted = False
    failed_requests = 0
    internal_errors = 0
    try:
        enabled = True
        kubectl("-n", "toggle", "set", "env", "deployment/auth-service", "AUTH_FAULT_INJECTION_ENABLED=true")
        kubectl("-n", "toggle", "rollout", "status", "deployment/auth-service", "--timeout=150s")
        base = smoke.forward("auth-service", 8001)
        if status(base) != 401:
            raise RuntimeError("auth não respondeu normalmente antes da falha")
        kubectl("-n", "toggle", "exec", "deployment/auth-service", "--", "/auth-service", "inject-failure")
        deadline = time.monotonic() + 300
        next_check = 0
        while time.monotonic() < deadline:
            try:
                code = status(base)
                if code == 503:
                    failed_requests += 1
                    if status(base, "/admin/keys", invalid_key, admin_headers) != 500:
                        raise RuntimeError("operação sintética não retornou HTTP 500")
                    internal_errors += 1
                elif code != 401:
                    raise RuntimeError("status inesperado do auth: " + str(code))
            except urllib.error.URLError:
                if marker() == before:
                    raise
            if time.monotonic() >= next_check:
                fired = fired or alert_active()
                restarted = marker() != before
                if fired and restarted:
                    break
                next_check = time.monotonic() + 3
            time.sleep(0.5)
        if not fired or not restarted or failed_requests < 20 or internal_errors < 20:
            raise RuntimeError(f"prova incompleta: 503={failed_requests}, 500={internal_errors}, alerta={fired}, rollout={restarted}")
        kubectl("-n", "toggle", "rollout", "status", "deployment/auth-service", "--timeout=150s")
        recovery = smoke.forward("auth-service", 8001)
        if status(recovery) != 401:
            raise RuntimeError("auth não recuperou comportamento normal após rollout")
        print(f"Falha real: {failed_requests} respostas 503 e {internal_errors} respostas 500; alerta ativo; responder reiniciou auth; resposta 401 restaurada.", flush=True)
    finally:
        if enabled:
            kubectl("-n", "toggle", "set", "env", "deployment/auth-service", "AUTH_FAULT_INJECTION_ENABLED-")
            kubectl("-n", "toggle", "rollout", "status", "deployment/auth-service", "--timeout=150s")
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        if not alert_active():
            print("Alerta resolvido; configuração opt-in removida e auth disponível.", flush=True)
            return
        time.sleep(5)
    raise RuntimeError("alerta não ficou inativo após a recuperação")


if __name__ == "__main__":
    try:
        main()
    finally:
        for process in smoke.PROCESSES:
            process.terminate()
        for process in smoke.PROCESSES:
            process.wait(timeout=10)
