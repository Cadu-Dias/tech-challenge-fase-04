#!/usr/bin/env python3
"""Verify that unsigned incoming HTTP and SQS contexts cannot select trace IDs."""
import json
import os
import subprocess
import time
import urllib.error
import uuid

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
    loki = smoke.forward("loki", 3100)
    services = {
        "auth-service": (smoke.forward("auth-service", 8001), "/validate"),
        "flag-service": (smoke.forward("flag-service", 8002), "/flags"),
        "targeting-service": (smoke.forward("targeting-service", 8003), "/rules/synthetic"),
    }
    query = {"query": '{service_name=~"auth-service|flag-service|targeting-service|analytics-service"}',
             "start": str(int((time.time() - 300) * 1_000_000_000)), "limit": "1000"}

    def traces():
        result = smoke.query(loki, "/loki/api/v1/query_range", query)
        seen = {}
        for stream in result["data"]["result"]:
            service = stream["stream"].get("service_name")
            for _, line in stream["values"]:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("trace_id"):
                    seen.setdefault(service, set()).add(event["trace_id"])
        return seen

    previous = traces()
    forged = uuid.uuid4().hex
    parent = "00-" + forged + "-0123456789abcdef-01"
    for service, (base, path) in services.items():
        try:
            smoke.request(base, path, headers={"traceparent": parent,
                                              "Authorization": "Bearer synthetic-invalid"})
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise
    message = json.dumps({"user_id": "synthetic", "flag_name": "synthetic",
                          "result": True, "timestamp": "2026-01-01T00:00:00Z"})
    attributes = json.dumps({"traceparent": {"DataType": "String", "StringValue": parent}})
    subprocess.run(
        smoke.KUBECTL + ["-n", "toggle", "exec", "deployment/localstack", "--", "awslocal",
                         "sqs", "send-message", "--queue-url",
                         "http://localhost:4566/000000000000/togglemaster-evaluation-events",
                         "--message-body", message, "--message-attributes", attributes],
        capture_output=True, check=True,
    )
    expected = set(services) | {"analytics-service"}
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        observed = traces()
        if any(forged in ids for ids in observed.values()):
            raise RuntimeError("contexto não assinado foi aceito")
        if all(observed.get(service, set()) - previous.get(service, set()) for service in expected):
            print("HTTP e SQS: contextos não assinados iniciaram novos traces.")
            return
        time.sleep(3)
    raise RuntimeError("não foi possível observar novos traces em todos os serviços")


if __name__ == "__main__":
    try:
        main()
    finally:
        for process in smoke.PROCESSES:
            process.terminate()
        for process in smoke.PROCESSES:
            process.wait(timeout=10)
