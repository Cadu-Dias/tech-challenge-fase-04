#!/usr/bin/env python3
"""Verify sanitized container logs across local namespaces and containers."""
import json
import os
import re
import subprocess
import time
import uuid

import smoke


def main():
    if not os.environ.get("KUBECONFIG"):
        raise RuntimeError("KUBECONFIG local obrigatório")
    server = subprocess.check_output(
        smoke.KUBECTL + ["config", "view", "--minify", "-o",
                         "jsonpath={.clusters[0].cluster.server}"], text=True
    )
    if not re.fullmatch(r"https://127\.0\.0\.1:\d+", server):
        raise RuntimeError("API Kubernetes não está no loopback")
    loki = smoke.forward("loki", 3100)
    suffix = uuid.uuid4().hex[:12]
    name = "log-probe-" + suffix
    containers = ["lp" + suffix + "-a", "lp" + suffix + "-b"]
    canary = "synthetic-secret-" + uuid.uuid4().hex
    untrusted_trace = uuid.uuid4().hex
    safe = {"event": "http.request", "route": "/evaluate", "method": "GET", "status": 200}
    unsafe = {"event": "sqs.process", "route": canary, "method": canary,
              "status": {"secret": canary}, "outcome": canary, "trace_id": untrusted_trace,
              "span_id": "0123456789abcdef", "password": canary, "service": canary}
    lines = [json.dumps(safe), json.dumps(unsafe), canary, json.dumps({"event": canary})]
    code = "import time; time.sleep(10); print(" + repr("\n".join(lines)) + ", flush=True); time.sleep(120)"
    created = []
    start = str(int((time.time() - 10) * 1_000_000_000))
    try:
        for namespace in ("toggle", "observability"):
            pod = {"apiVersion": "v1", "kind": "Pod",
                   "metadata": {"name": name, "namespace": namespace},
                   "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                            "containers": [{"name": container, "image": "python:3.11-slim",
                                            "imagePullPolicy": "Never", "command": ["python", "-c", code]}
                                           for container in containers]}}
            subprocess.run(smoke.KUBECTL + ["create", "-f", "-"], input=json.dumps(pod),
                           text=True, capture_output=True, check=True)
            created.append(namespace)
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            result = smoke.query(loki, "/loki/api/v1/query_range", {
                "query": '{service_name=~"' + "|".join(containers) + '"}',
                "start": start, "limit": "100",
            })
            text = json.dumps(result)
            if canary in text or untrusted_trace in text or "0123456789abcdef" in text:
                raise RuntimeError("dado adversarial sintético chegou ao Loki")
            observed = set()
            for stream in result["data"]["result"]:
                labels = stream["stream"]
                for _, line in stream["values"]:
                    body = json.loads(line)
                    if body == safe or body == {"event": "sqs.process"}:
                        observed.add((labels.get("k8s_namespace_name"),
                                      labels.get("service_name"), body["event"]))
                    else:
                        raise RuntimeError("evento inesperado ou não sanitizado no Loki")
            expected = {(namespace, container, event)
                        for namespace in ("toggle", "observability")
                        for container in containers
                        for event in ("http.request", "sqs.process")}
            if observed == expected:
                print("Loki local: dois namespaces, quatro containers, oito eventos sanitizados; canários ausentes.")
                return
            time.sleep(3)
        raise RuntimeError("eventos sintéticos ausentes no Loki: " + repr(expected - observed))
    finally:
        for namespace in created:
            subprocess.run(smoke.KUBECTL + ["-n", namespace, "delete", "pod", name,
                                           "--ignore-not-found", "--wait=false"],
                           capture_output=True, check=True)


if __name__ == "__main__":
    try:
        main()
    finally:
        for process in smoke.PROCESSES:
            process.terminate()
        for process in smoke.PROCESSES:
            process.wait(timeout=10)
