#!/usr/bin/env python3
"""Copy only the synthetic webhook token and public CA to local Grafana."""
import base64
import json
import os
import subprocess

import smoke


def normalize_token(encoded):
    token = base64.b64decode(encoded, validate=True).strip()
    if len(token) < 32 or any(ch < 33 or ch > 126 for ch in token):
        raise ValueError("Token local de webhook inválido para cabeçalho HTTP")
    return token


def main():
    if not os.environ.get("KUBECONFIG"):
        raise RuntimeError("KUBECONFIG local obrigatório")
    server = subprocess.check_output(
        smoke.KUBECTL + ["config", "view", "--minify", "-o",
                         "jsonpath={.clusters[0].cluster.server}"], text=True
    )
    if not server.startswith("https://127.0.0.1:"):
        raise RuntimeError("API Kubernetes não está no loopback")

    def source(name, key):
        value = subprocess.check_output(
            smoke.KUBECTL + ["-n", "toggle", "get", "secret", name,
                             "-o", 'go-template={{index .data "' + key + '"}}'], text=True
        ).strip()
        if not value:
            raise RuntimeError("Secret de origem sem chave esperada")
        return value

    token = normalize_token(source("toggle-self-healing-webhook", "token"))
    data = {"token": base64.b64encode(token).decode(),
            "tls.crt": source("toggle-self-healing-tls", "tls.crt")}
    existing = subprocess.run(
        smoke.KUBECTL + ["-n", "observability", "get", "secret", "self-healing-client",
                         "--ignore-not-found", "-o", "json"],
        capture_output=True, check=True, text=True,
    )
    if existing.stdout.strip():
        current = json.loads(existing.stdout)
        if current["data"] != data:
            old = current["data"]
            if old.get("tls.crt") != data["tls.crt"] or set(old) != set(data) or \
                    base64.b64decode(old["token"], validate=True).strip() != token:
                raise RuntimeError("Secret existente difere; revise rotação antes de substituir")
            current["data"] = data
            subprocess.run(smoke.KUBECTL + ["replace", "-f", "-"], input=json.dumps(current),
                           text=True, capture_output=True, check=True)
            print("Token sintético do Grafana normalizado para cabeçalho HTTP.")
            return
        print("Secret local do Grafana já corresponde ao responder.")
        return
    secret = {"apiVersion": "v1", "kind": "Secret",
              "metadata": {"name": "self-healing-client", "namespace": "observability"},
              "type": "Opaque", "data": data}
    subprocess.run(smoke.KUBECTL + ["create", "-f", "-"],
                   input=json.dumps(secret), text=True, check=True)


if __name__ == "__main__":
    main()
