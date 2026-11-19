#!/usr/bin/env python3
"""Verify renewed local webhook TLS and token without restarting a workload."""
import base64
import json
import os
import ssl
import subprocess
import urllib.error
import urllib.request

import smoke


def main():
    if not os.environ.get("KUBECONFIG"):
        raise RuntimeError("KUBECONFIG local obrigatório")
    server = subprocess.check_output(
        smoke.KUBECTL + ["config", "view", "--minify", "-o",
                         "jsonpath={.clusters[0].cluster.server}"], text=True
    )
    if not server.startswith("https://127.0.0.1:"):
        raise RuntimeError("API Kubernetes não está no loopback")

    def secret(namespace, name, key):
        value = subprocess.check_output(
            smoke.KUBECTL + ["-n", namespace, "get", "secret", name,
                             "-o", 'go-template={{index .data "' + key + '"}}'], text=True
        ).strip()
        return base64.b64decode(value, validate=True)

    certificate = secret("toggle", "toggle-self-healing-tls", "tls.crt")
    if certificate != secret("observability", "self-healing-client", "tls.crt"):
        raise RuntimeError("CA do Grafana diverge do certificado do webhook")
    token = secret("toggle", "toggle-self-healing-webhook", "token").strip()
    if token != secret("observability", "self-healing-client", "token"):
        raise RuntimeError("Token do Grafana diverge do token do webhook")
    context = ssl.create_default_context(cadata=certificate.decode("ascii"))
    base = smoke.forward("toggle-self-healing", 8080).replace("http://", "https://", 1)
    body = json.dumps({"alerts": [{"status": "resolved"}]}).encode()
    for authorization, expected in (("", 401), ("Bearer " + token.decode("ascii"), 200)):
        request = urllib.request.Request(base + "/webhook", data=body,
                                         headers={"Authorization": authorization,
                                                  "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, context=context, timeout=10) as response:
                status = response.status
                result = json.load(response)
        except urllib.error.HTTPError as error:
            status, result = error.code, None
        if status != expected or (status == 200 and result != {"results": ["ignored"]}):
            raise RuntimeError("TLS, autenticação ou tratamento de resolução falhou")
    print("Webhook local validou TLS e token; alerta resolved não reiniciou serviço.")


if __name__ == "__main__":
    try:
        main()
    finally:
        for process in smoke.PROCESSES:
            process.terminate()
        for process in smoke.PROCESSES:
            process.wait(timeout=10)
