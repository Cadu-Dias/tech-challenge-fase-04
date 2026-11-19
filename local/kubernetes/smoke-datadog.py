#!/usr/bin/env python3
"""Verify that an application trace reaches the configured Datadog exporter."""
import os
import re
import subprocess
import time
import urllib.request

import smoke


def sent_count(base):
    with urllib.request.urlopen(base + "/metrics", timeout=5) as response:
        metrics = response.read().decode()
    match = re.search(
        r'^otelcol_exporter_sent_spans\{[^}]*exporter="otlphttp/datadog"[^}]*\}\s+([0-9.]+)',
        metrics, re.MULTILINE,
    )
    return float(match.group(1)) if match else 0


if __name__ == "__main__":
    try:
        if not os.environ.get("KUBECONFIG"):
            raise RuntimeError("KUBECONFIG local obrigatório")
        server = subprocess.check_output(
            smoke.KUBECTL + ["config", "view", "--minify", "-o",
                             "jsonpath={.clusters[0].cluster.server}"], text=True
        )
        if not server.startswith("https://127.0.0.1:"):
            raise RuntimeError("API Kubernetes não está no loopback")
        metrics = smoke.forward("otel-collector", 8888, namespace="observability", kind="deployment")
        before = sent_count(metrics)
        smoke.main()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if sent_count(metrics) > before:
                print("Exportador Datadog confirmou envio de spans da aplicação local.")
                break
            time.sleep(3)
        else:
            raise RuntimeError("exportador Datadog não confirmou novos spans da aplicação")
    finally:
        for process in smoke.PROCESSES:
            process.terminate()
        for process in smoke.PROCESSES:
            process.wait(timeout=10)
