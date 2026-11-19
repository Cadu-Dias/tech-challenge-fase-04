#!/usr/bin/env python3
"""Verify authenticated, source-isolated auth metrics in the local cluster."""
import base64
import http.client
import os
import socket
import ssl
import subprocess
import time
import urllib.error

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
    secret = lambda key: base64.b64decode(subprocess.check_output(
        smoke.KUBECTL + ["-n", "observability", "get", "secret", "telemetry-gateway-auth",
                         "-o", 'go-template={{index .data "' + key + '"}}'], text=True
    ))
    context = ssl.create_default_context(cadata=secret("tls.crt").decode("ascii"))
    shared = secret("token").decode("ascii").strip()
    port = int(smoke.forward("otel-collector", 4319, namespace="observability").rsplit(":", 1)[1])
    for token in ("", "synthetic-invalid", shared):
        authorization = "Authorization: Bearer " + token + "\r\n" if token else ""
        with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
            with context.wrap_socket(raw, server_hostname="otel-collector.observability.svc.cluster.local") as conn:
                body = b'{"resourceMetrics":[]}'
                headers = ("POST /v1/metrics HTTP/1.1\r\nHost: otel-collector\r\n"
                           "Content-Type: application/json\r\n" + authorization +
                           "Content-Length: " + str(len(body)) + "\r\nConnection: close\r\n\r\n")
                conn.sendall(headers.encode() + body)
                response = http.client.HTTPResponse(conn)
                response.begin()
                if response.status != 401:
                    raise RuntimeError("receptor de métricas auth aceitou token não dedicado")
    auth = smoke.forward("auth-service", 8001)
    prometheus = smoke.forward("prometheus", 9090)
    try:
        smoke.request(auth, "/validate", headers={"Authorization": "Bearer synthetic-invalid"})
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise
    query = {'query': 'http_server_request_duration_seconds_count{job="auth-metrics",service_name="auth-service"}'}
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = smoke.query(prometheus, "/api/v1/query", query)
        if result["data"]["result"]:
            print("Métricas auth recebidas no job dedicado; tokens ausente, inválido e compartilhado rejeitados.")
            return
        time.sleep(3)
    raise RuntimeError("métricas auth ausentes no job dedicado")


if __name__ == "__main__":
    try:
        main()
    finally:
        for process in smoke.PROCESSES:
            process.terminate()
        for process in smoke.PROCESSES:
            process.wait(timeout=10)
