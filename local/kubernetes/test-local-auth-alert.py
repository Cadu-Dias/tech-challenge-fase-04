#!/usr/bin/env python3
"""Ensure the local auth rule ignores series from the shared Collector."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
expression = subprocess.check_output(
    ["ruby", "-ryaml", "-e",
     'x=YAML.safe_load(STDIN.read); puts x["groups"][0]["rules"][0]["data"][0]["model"]["expr"]'],
    input=subprocess.check_output(["ruby", str(root / "local/kubernetes/local-auth-alert.rb")]),
).decode().strip()
rules = {"groups": [{"name": "local", "interval": "30s",
                     "rules": [{"record": "auth:local_ratio", "expr": expression}]}]}
metric = 'http_server_request_duration_seconds_count'
tests = {"rule_files": ["rules.json"], "evaluation_interval": "30s", "tests": [
    {"name": "shared source cannot trigger", "interval": "30s",
     "input_series": [{"series": metric + '{job="otel-collector",service_name="auth-service",http_response_status_code="500"}',
                       "values": "0 30 60 90 120"}],
     "promql_expr_test": [{"expr": "auth:local_ratio", "eval_time": "2m", "exp_samples": []}]},
    {"name": "shared source cannot change dedicated ratio", "interval": "30s",
     "input_series": [
         {"series": metric + '{job="auth-metrics",service_name="auth-service",http_response_status_code="200"}',
          "values": "0 10 20 30 40"},
         {"series": metric + '{job="otel-collector",service_name="auth-service",http_response_status_code="500"}',
          "values": "0 30 60 90 120"}],
     "promql_expr_test": [{"expr": "auth:local_ratio", "eval_time": "2m",
                           "exp_samples": [{"labels": "auth:local_ratio", "value": 0}]}]},
]}
with tempfile.TemporaryDirectory(prefix="local-auth-alert-", dir=root / "local/kubernetes") as directory:
    os.chmod(directory, 0o755)
    Path(directory, "rules.json").write_text(json.dumps(rules))
    Path(directory, "test.json").write_text(json.dumps(tests))
    subprocess.run(["docker", "run", "--rm", "--network", "none", "--pull", "never",
                    "-v", directory + ":/rules:ro", "--entrypoint", "promtool",
                    "prom/prometheus:v2.54.1", "test", "rules", "/rules/test.json"], check=True)
