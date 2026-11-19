#!/usr/bin/env python3
"""Adversarial synthetic test of the exact Datadog filtering processors."""
import json
import re
import subprocess
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

root = Path(__file__).resolve().parents[2]
pipeline = root / "local/kubernetes/datadog-pipeline.yaml"
data = json.loads(subprocess.check_output(
    ["ruby", "-rjson", "-ryaml", "-e",
     "puts JSON.generate(YAML.load_file(ARGV[0]))", str(pipeline)], text=True
))
data["receivers"] = {"otlp": {"protocols": {"http": {"endpoint": "0.0.0.0:4318"}}}}
data["exporters"] = {"debug/sanitized": {"verbosity": "detailed"}}
data["service"]["pipelines"]["traces"] = {
    "receivers": ["otlp"], "exporters": ["forward/datadog"]
}
data["service"]["pipelines"]["traces/datadog"]["exporters"] = ["debug/sanitized"]
canary = "synthetic-sensitive-" + uuid.uuid4().hex
trace_id = uuid.uuid4().hex
now = time.time_ns()
value = lambda text: {"stringValue": text}
span = {"traceId": trace_id, "spanId": "0123456789abcdef", "name": "/evaluate",
        "startTimeUnixNano": str(now - 1000000), "endTimeUnixNano": str(now),
        "traceState": "demo=" + canary, "attributes": [{"key": "password", "value": value(canary)}],
        "status": {"code": 2, "message": canary}}
event_span = dict(span, spanId="1123456789abcdef",
                  events=[{"name": canary, "timeUnixNano": str(now)}])
link_span = dict(span, spanId="2123456789abcdef",
                 links=[{"traceId": uuid.uuid4().hex, "spanId": "0123456789abcdef"}])
unknown_span = dict(span, spanId="3123456789abcdef", name=canary)
resource = {"schemaUrl": "https://example.invalid/" + canary,
            "resource": {"attributes": [{"key": "service.name", "value": value("evaluation-service")},
                                        {"key": "password", "value": value(canary)}]},
            "scopeSpans": [{"schemaUrl": "https://example.invalid/" + canary,
                            "scope": {"name": canary, "version": canary,
                                      "attributes": [{"key": "password", "value": value(canary)}]},
                            "spans": [span, event_span, link_span, unknown_span]}]}
untrusted = {"resource": {"attributes": [{"key": "service.name", "value": value(canary)}]},
             "scopeSpans": [{"spans": [span]}]}
payload = json.dumps({"resourceSpans": [resource, untrusted]}).encode()
name = "sanitize-test-" + uuid.uuid4().hex[:12]

with tempfile.TemporaryDirectory(prefix="toggle-sanitize-", dir=root / "local/kubernetes") as directory:
    config = Path(directory) / "config.json"
    output = Path(directory) / "output"
    config.write_text(json.dumps(data))
    with output.open("w") as log:
        process = subprocess.Popen(
            ["docker", "run", "--rm", "--name", name, "-p", "127.0.0.1::4318",
             "-v", str(config) + ":/config.json:ro",
             "otel/opentelemetry-collector-contrib:0.161.0", "--config=/config.json"],
            stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Collector de teste terminou antes de iniciar:\n" + output.read_text())
                port = subprocess.run(["docker", "port", name, "4318/tcp"],
                                      capture_output=True, text=True)
                if port.returncode == 0 and port.stdout.strip():
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("Collector de teste não abriu porta local")
            endpoint = "http://127.0.0.1:" + port.stdout.strip().rsplit(":", 1)[1] + "/v1/traces"
            request = urllib.request.Request(endpoint, data=payload,
                                             headers={"Content-Type": "application/json"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for attempt in range(20):
                try:
                    with opener.open(request, timeout=5) as response:
                        if response.status != 200:
                            raise RuntimeError("Collector de teste rejeitou OTLP")
                    break
                except urllib.error.URLError:
                    if attempt == 19:
                        raise
                    time.sleep(0.5)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                text = output.read_text()
                if '"otelcol.component.id": "debug/sanitized"' in text:
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("exportador de teste não recebeu dados")
            if canary in text:
                raise RuntimeError("canário em campo sensível permaneceu após sanitização")
            names = re.findall(r"^[ \t]*Name[ \t]*:[ \t]*(.*)$", text, re.MULTILINE)
            if names != ["/evaluate"]:
                raise RuntimeError("filtro de spans não aceitou apenas a operação permitida")
            for label in ("Resource SchemaURL", "ScopeSpans SchemaURL"):
                matches = re.findall(r"^" + label + r": (.*)$", text, re.MULTILINE)
                if not matches or any(matches):
                    raise RuntimeError(label + " não foi removida")
            print("Sanitização adversarial: apenas span permitido, sem canários, eventos ou links.")
        except Exception:
            print(output.read_text())
            raise
        finally:
            subprocess.run(["docker", "stop", "--time", "3", name], capture_output=True)
            process.wait(timeout=10)
