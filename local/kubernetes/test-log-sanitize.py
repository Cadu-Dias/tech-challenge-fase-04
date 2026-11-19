#!/usr/bin/env python3
"""Test the actual filelog pipeline against adversarial synthetic container logs."""
import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "local/kubernetes/agent.yaml"
data = json.loads(subprocess.check_output(
    ["ruby", "-rjson", "-ryaml", "-e", "puts JSON.generate(YAML.load_file(ARGV[0]))", str(CONFIG)],
    text=True,
))
data["extensions"] = {}
data["exporters"] = {"debug/sanitized": {"verbosity": "detailed"}}
data["service"]["extensions"] = []
data["service"]["pipelines"]["logs"]["exporters"] = ["debug/sanitized"]
canary = "synthetic-sensitive-" + uuid.uuid4().hex
trace_id = uuid.uuid4().hex
untrusted_id = uuid.uuid4().hex
name = "log-sanitize-test-" + uuid.uuid4().hex[:12]

with tempfile.TemporaryDirectory(prefix="log-sanitize-", dir=ROOT / "local/kubernetes") as directory:
    directory = Path(directory)
    logs = directory / "pods"
    events = [
        ("toggle", "auth-service", {
            "event": "http.request", "route": "/validate", "method": "GET", "status": 200,
            "trace_id": trace_id, "password": canary, "service": canary,
        }),
        ("observability", "log-probe", {
            "event": "http.request", "route": canary, "method": canary,
            "status": {"secret": canary}, "outcome": canary,
            "trace_id": untrusted_id, "span_id": "0123456789abcdef", "password": canary,
        }),
    ]
    for namespace, container, event in events:
        path = logs / f"{namespace}_probe_{uuid.uuid4()}" / container / "0.log"
        path.parent.mkdir(parents=True)
        path.write_text(
            "2026-01-01T00:00:00.000000000Z stdout F " + json.dumps(event) + "\n"
            "2026-01-01T00:00:00.000000001Z stdout F " + canary + "\n"
            "2026-01-01T00:00:00.000000002Z stdout F " + json.dumps({"event": canary}) + "\n"
        )
    config = directory / "config.json"
    config.write_text(json.dumps(data))
    output = directory / "output"
    with output.open("w") as log:
        subprocess.run(
            ["docker", "create", "--network", "none", "--name", name,
             "-v", str(config) + ":/config.json:ro",
             "-v", str(logs) + ":/var/log/pods:ro",
             "otel/opentelemetry-collector-contrib:0.161.0", "--config=/config.json"],
            check=True, stdout=subprocess.DEVNULL,
        )
        process = subprocess.Popen(["docker", "start", "-a", name], stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                text = output.read_text()
                if process.poll() is not None:
                    raise RuntimeError("Collector de teste terminou antes de concluir")
                if text.count("Body: Map(") >= 2:
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("eventos sintéticos não chegaram ao exportador")
            if canary in text or untrusted_id in text:
                raise RuntimeError("dado proibido permaneceu no pipeline")
            if text.count("Body: Map(") != 2 or trace_id not in text or '"status":200' not in text.replace(" ", ""):
                raise RuntimeError("filtro descartou eventos válidos ou aceitou evento proibido")
            if "toggle" not in text or "observability" not in text:
                raise RuntimeError("metadados de namespace ausentes")
            print("Logs isolados: dois namespaces aceitos; dados adversariais removidos.")
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=True)
            process.wait(timeout=10)
