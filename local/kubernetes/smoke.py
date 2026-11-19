#!/usr/bin/env python3
"""Synthetic smoke test for the disposable kind stack; no external traffic."""
import json
import os
import re
import select
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

CONTEXT = "kind-toggle-observability"
KUBECTL = ["kubectl", "--context", CONTEXT]
PROCESSES = []


def forward(name, port, namespace="toggle", kind="service"):
    process = subprocess.Popen(
        KUBECTL + ["-n", namespace, "port-forward", "--address", "127.0.0.1",
                   kind + "/" + name, ":" + str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    PROCESSES.append(process)
    if not select.select([process.stdout], [], [], 30)[0]:
        raise RuntimeError("port-forward indisponível: " + name)
    line = process.stdout.readline()
    match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", line)
    if not match:
        raise RuntimeError("port-forward falhou: " + name)
    return "http://127.0.0.1:" + match.group(1)


def request(base, path, body=None, token=None, headers=None):
    request_headers = dict(headers or {})
    if token:
        request_headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=request_headers)
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def query(base, path, params):
    return request(base, path + "?" + urllib.parse.urlencode(params))


def main():
    if not os.environ.get("KUBECONFIG"):
        raise RuntimeError("KUBECONFIG local obrigatório")
    server = subprocess.check_output(
        KUBECTL + ["config", "view", "--minify", "-o",
                   "jsonpath={.clusters[0].cluster.server}"], text=True
    )
    if not re.fullmatch(r"https://127\.0\.0\.1:\d+", server):
        raise RuntimeError("API Kubernetes não está no loopback")

    flag = forward("flag-service", 8002)
    targeting = forward("targeting-service", 8003)
    evaluation = forward("evaluation-service", 8004)
    prometheus = forward("prometheus", 9090)
    loki = forward("loki", 3100)
    tempo = forward("tempo", 3200)
    start_ns = str(int((time.time() - 300) * 1_000_000_000))
    log_query = {
        "query": '{service_name=~"evaluation-service|analytics-service"}',
        "start": start_ns,
        "limit": "500",
    }
    previous_logs = query(loki, "/loki/api/v1/query_range", log_query)
    previous_traces = set()
    for stream in previous_logs["data"]["result"]:
        if stream["stream"].get("service_name") != "evaluation-service":
            continue
        for _, line in stream["values"]:
            try:
                previous_traces.add(json.loads(line).get("trace_id"))
            except json.JSONDecodeError:
                continue
    name = "synthetic-" + uuid.uuid4().hex[:12]
    token = "local-service-key"
    request(flag, "/flags", {"name": name, "description": "local synthetic",
                             "is_enabled": True}, token)
    request(targeting, "/rules", {"flag_name": name, "is_enabled": True,
                                   "rules": {"type": "PERCENTAGE", "value": 100}}, token)
    supplied_trace = uuid.uuid4().hex
    result = request(evaluation, "/evaluate?" + urllib.parse.urlencode(
        {"user_id": "synthetic-user", "flag_name": name}),
        headers={"traceparent": "00-" + supplied_trace + "-0123456789abcdef-01"})
    if result.get("result") is not True:
        raise RuntimeError("avaliação sintética não retornou true")

    deadline = time.monotonic() + 180
    trace_id = None
    found = set()
    metric = False
    log_services = set()
    sqs_parent = False
    while time.monotonic() < deadline:
        metric_result = query(prometheus, "/api/v1/query", {
            "query": 'http_server_request_duration_seconds_count{service_name="evaluation-service"}'
        })
        metric = bool(metric_result["data"]["result"])
        logs = query(loki, "/loki/api/v1/query_range", log_query)
        events = []
        for stream in logs["data"]["result"]:
            service = stream["stream"].get("service_name")
            for _, line in stream["values"]:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                events.append((service, event))
                if service == "evaluation-service" and event.get("route") == "/evaluate":
                    candidate = event.get("trace_id")
                    if not trace_id and candidate and candidate not in previous_traces:
                        trace_id = candidate
        log_services = {service for service, event in events
                        if trace_id and event.get("trace_id") == trace_id}
        if trace_id:
            try:
                trace = request(tempo, "/api/traces/" + trace_id)
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
            else:
                spans = {}
                for resource in trace.get("batches", []):
                    for attr in resource.get("resource", {}).get("attributes", []):
                        if attr.get("key") == "service.name":
                            found.add(attr.get("value", {}).get("stringValue"))
                    for scope in resource.get("scopeSpans", []):
                        for span in scope.get("spans", []):
                            spans[span.get("name")] = span
                producer = spans.get("sqs send", {})
                consumer = spans.get("sqs process", {})
                sqs_parent = bool(producer.get("spanId") and
                                  producer["spanId"] == consumer.get("parentSpanId"))
        if metric and {"evaluation-service", "analytics-service"} <= log_services and {
            "evaluation-service", "flag-service", "targeting-service",
            "auth-service", "analytics-service"
        } <= found and sqs_parent:
            break
        time.sleep(5)
    print(json.dumps({"synthetic_evaluation": True, "metric": metric,
                      "new_trace_id": bool(trace_id and trace_id != supplied_trace),
                      "sqs_parent_child": sqs_parent,
                      "correlated_log_services": sorted(log_services),
                      "trace_services": sorted(found)}))
    expected = {"evaluation-service", "flag-service", "targeting-service",
                "auth-service", "analytics-service"}
    if not metric or not trace_id or trace_id == supplied_trace or not sqs_parent or not expected <= found or not {
        "evaluation-service", "analytics-service"
    } <= log_services:
        raise RuntimeError("observabilidade ou correlação incompleta")


if __name__ == "__main__":
    try:
        main()
    finally:
        for proc in PROCESSES:
            proc.terminate()
        for proc in PROCESSES:
            proc.wait(timeout=10)
