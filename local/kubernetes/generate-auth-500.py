#!/usr/bin/env python3
"""Generate bounded synthetic auth HTTP 500s; never change notification policies."""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

import smoke


ALERT = "Auth com taxa elevada de erros 5xx"
LABELS = {
    "alertname": ALERT, "namespace": "toggle",
    "service": "auth-service", "severity": "critical",
}
PAGERDUTY = "local-pagerduty"
PAYLOAD = json.dumps({"name": "synthetic-" + "x" * 120}).encode()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


CLIENT = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), NoRedirect(),
)


def kubectl(*args):
    return subprocess.check_output(
        smoke.KUBECTL + list(args), text=True, stderr=subprocess.PIPE,
    )


def request(base, path, auth, data=None):
    headers = {"Authorization": auth}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, headers=headers, data=data)
    with CLIENT.open(req, timeout=5) as response:
        return json.load(response)


def matches(route):
    if route.get("matchers") or route.get("match") or route.get("match_re"):
        raise RuntimeError("Formato legado de matchers não suportado; revise a política.")
    for key, operator, value in route.get("object_matchers", []):
        actual = LABELS.get(key, "")
        if operator == "=":
            matched = actual == value
        elif operator == "!=":
            matched = actual != value
        else:
            raise RuntimeError("Matcher regex não suportado; revise a política.")
        if not matched:
            return False
    return True


def receivers_for(route, inherited=None):
    receiver = route.get("receiver") or inherited
    selected = []
    for child in route.get("routes", []):
        if matches(child):
            selected.extend(receivers_for(child, receiver))
            if not child.get("continue", False):
                break
    return selected or [receiver]


def generate(base, auth, duration, interval, reconnect=None):
    deadline = time.monotonic() + duration
    count = 0
    connection_errors = 0
    while time.monotonic() < deadline:
        try:
            request(base, "/admin/keys", auth, PAYLOAD)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if code != 500:
                raise RuntimeError(
                    f"Esperado HTTP 500, recebido {code}; geração interrompida."
                ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            connection_errors += 1
            if reconnect is None or connection_errors > 3:
                raise RuntimeError(
                    "Conexão auth indisponível; geração interrompida após tentativas limitadas."
                ) from None
            print("Conexão auth interrompida; restabelecendo port-forward.", flush=True)
            time.sleep(min(3, max(0, deadline - time.monotonic())))
            if time.monotonic() < deadline:
                base = reconnect()
            continue
        else:
            raise RuntimeError("Resposta de sucesso inesperada; geração interrompida.")
        connection_errors = 0
        count += 1
        if count == 1 or count % 10 == 0:
            print(f"HTTP 500 sintéticos: {count}", flush=True)
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    return count


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true",
                        help="generate errors; without this flag only inspect configuration")
    parser.add_argument("--allow-external-notifications", action="store_true",
                        help="acknowledge notifications to all matching existing receivers")
    parser.add_argument("--require-pagerduty", action="store_true",
                        help="also require PagerDuty in the notification policy tree")
    parser.add_argument("--duration", type=int, default=300, help="seconds, from 120 to 900")
    parser.add_argument("--interval", type=float, default=1, help="seconds, from 0.5 to 2")
    args = parser.parse_args()
    if not 120 <= args.duration <= 900:
        parser.error("--duration must be between 120 and 900")
    if not 0.5 <= args.interval <= 2:
        parser.error("--interval must be between 0.5 and 2")
    if args.run and not args.allow_external_notifications:
        parser.error("--run requires --allow-external-notifications")
    return args


def main():
    args = arguments()
    if not os.environ.get("KUBECONFIG"):
        raise RuntimeError("KUBECONFIG local obrigatório.")
    server = kubectl("config", "view", "--minify", "-o",
                     "jsonpath={.clusters[0].cluster.server}")
    if not re.fullmatch(r"https://127\.0\.0\.1:\d+", server):
        raise RuntimeError("Use somente a API do cluster kind local no loopback.")
    grafana_secret = json.loads(kubectl(
        "-n", "observability", "get", "secret", "grafana-admin", "-o", "json",
    ))["data"]
    auth = "Basic " + base64.b64encode(
        base64.b64decode(grafana_secret["username"]) + b":" +
        base64.b64decode(grafana_secret["password"])
    ).decode()
    grafana = smoke.forward("grafana", 3000, namespace="observability")
    policy = request(grafana, "/api/v1/provisioning/policies", auth)
    receivers = receivers_for(policy)
    print("Destinos atuais do alerta auth: " + ", ".join(receivers), flush=True)
    if PAGERDUTY not in receivers and args.require_pagerduty:
        raise RuntimeError(
            "PagerDuty não recebe este alerta. Configure uma rota exata para "
            "'local-pagerduty' antes das rotas auth existentes, com continue=true. "
            "Nenhum HTTP 500 gerado; políticas não foram alteradas."
        )
    if PAGERDUTY not in receivers:
        print("PagerDuty não identificado nesta árvore; geração não altera nem "
              "garante roteamento/entrega.", flush=True)
    if args.require_pagerduty:
        contacts = request(grafana, "/api/v1/provisioning/contact-points", auth)
        if not any(p.get("name") == PAGERDUTY and p.get("type") == "pagerduty"
                   for p in contacts):
            raise RuntimeError("Contact point local-pagerduty ausente ou de tipo incorreto.")
    if not args.run:
        print("Pré-verificação OK; nenhum erro ou teste externo enviado.")
        return
    # This constraint rejects the synthetic name before any API key is inserted.
    column_limit = kubectl(
        "-n", "toggle", "exec", "deployment/postgres", "--",
        "psql", "-U", "togglemaster", "-d", "auth_db", "-Atc",
        "SELECT character_maximum_length FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='api_keys' AND column_name='name'",
    ).strip()
    if column_limit != "100":
        raise RuntimeError("Limite VARCHAR(100) não confirmado; geração cancelada.")
    auth_secret = json.loads(kubectl(
        "-n", "toggle", "get", "secret", "auth-secret", "-o", "json",
    ))["data"]
    master_key = base64.b64decode(auth_secret["MASTER_KEY"]).decode()
    base = smoke.forward("auth-service", 8001)
    print("Gerando 500; Ctrl+C interrompe. Não altera pods, políticas ou opt-in.", flush=True)
    count = generate(
        base, "Bearer " + master_key, args.duration, args.interval,
        reconnect=lambda: smoke.forward("auth-service", 8001),
    )
    print(f"Concluído: {count} respostas 500. Entrega PagerDuty não verificada.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nGeração interrompida; nenhuma alteração de configuração a desfazer.")
    except (RuntimeError, urllib.error.URLError, TimeoutError, OSError,
            ValueError, KeyError, subprocess.CalledProcessError) as error:
        # Never print HTTP bodies, subprocess output, headers or credentials.
        if isinstance(error, RuntimeError):
            print(str(error), file=sys.stderr)
        else:
            print("Falha na pré-verificação ou conexão; geração interrompida "
                  f"({type(error).__name__}).", file=sys.stderr)
        sys.exit(1)
    finally:
        for process in smoke.PROCESSES:
            process.terminate()
        for process in smoke.PROCESSES:
            process.wait(timeout=10)
