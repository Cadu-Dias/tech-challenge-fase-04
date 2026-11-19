#!/usr/bin/env python3
"""Verify that the local OTLP receiver refuses unauthenticated traffic."""
import base64
import http.client
import os
import socket
import ssl
import subprocess

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
    cert = subprocess.check_output(
        smoke.KUBECTL + ["-n", "observability", "get", "secret", "telemetry-gateway-auth",
                         "-o", 'go-template={{index .data "tls.crt"}}'], text=True
    )
    context = ssl.create_default_context(cadata=base64.b64decode(cert).decode("ascii"))
    port = int(smoke.forward("otel-collector", 4318, namespace="observability").rsplit(":", 1)[1])
    for authorization in ("", "Authorization: Bearer synthetic-invalid\r\n"):
        with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
            with context.wrap_socket(raw, server_hostname="otel-collector.observability.svc.cluster.local") as conn:
                body = b'{"resourceSpans":[]}'
                headers = ("POST /v1/traces HTTP/1.1\r\nHost: otel-collector\r\n"
                           "Content-Type: application/json\r\n" + authorization +
                           "Content-Length: " + str(len(body)) + "\r\nConnection: close\r\n\r\n")
                conn.sendall(headers.encode() + body)
                response = http.client.HTTPResponse(conn)
                response.begin()
                if response.status != 401:
                    raise RuntimeError("OTLP aceitou uma origem não autenticada")
    print("OTLP: TLS verificado e requisições sem token ou com token inválido rejeitadas.")


if __name__ == "__main__":
    try:
        main()
    finally:
        for process in smoke.PROCESSES:
            process.terminate()
        for process in smoke.PROCESSES:
            process.wait(timeout=10)
