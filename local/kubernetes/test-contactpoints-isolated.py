#!/usr/bin/env python3
"""Exercise Grafana PagerDuty/Teams integrations against loopback-only mocks."""
import json
import subprocess
import tempfile
import time
import uuid


MOCK = r'''
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/pagerduty":
            valid = body.get("routing_key") == "synthetic-only" and body.get("event_action") == "trigger"
        elif self.path == "/teams":
            attachments = body.get("attachments", [])
            valid = (
                body.get("type") == "message" and len(attachments) == 1
                and attachments[0].get("contentType") == "application/vnd.microsoft.card.adaptive"
                and attachments[0].get("content", {}).get("type") == "AdaptiveCard"
                and bool(attachments[0].get("content", {}).get("version"))
                and any(item.get("type") == "TextBlock"
                        for item in attachments[0].get("content", {}).get("body", []))
            )
        else:
            valid = False
        print(json.dumps({"path": self.path, "valid": valid}), flush=True)
        self.send_response(202 if valid else 400)
        self.end_headers()
HTTPServer(("127.0.0.1", 19999), Handler).serve_forever()
'''

CLIENT = r'''
import base64, json, time, urllib.error, urllib.request
url = "http://127.0.0.1:3000"
for _ in range(90):
    try:
        with urllib.request.urlopen(url + "/api/health", timeout=2): break
    except (urllib.error.URLError, TimeoutError): time.sleep(1)
else: raise RuntimeError("Grafana isolado não iniciou")
body = {"alert": {"labels": {"alertname": "synthetic-test"}, "annotations": {"summary": "synthetic"}},
        "receivers": [
            {"name": "synthetic-pagerduty", "grafana_managed_receiver_configs": [
                {"name":"synthetic-pagerduty","type":"pagerduty",
                 "settings":{"url":"http://127.0.0.1:19999/pagerduty","integrationKey":"synthetic-only"}}]},
            {"name": "synthetic-teams", "grafana_managed_receiver_configs": [
                {"name":"synthetic-teams","type":"teams",
                 "settings":{"url":"http://127.0.0.1:19999/teams"}}]}]}
authorization = base64.b64encode(b"synthetic:synthetic-local-only").decode()
request = urllib.request.Request(url + "/api/alertmanager/grafana/config/api/v1/receivers/test",
    data=json.dumps(body).encode(), headers={"Content-Type":"application/json","Authorization":"Basic "+authorization})
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
except urllib.error.HTTPError as error:
    raise RuntimeError("API de teste isolada: " + error.read().decode()) from None
configs = [c for r in result.get("receivers", []) for c in r.get("grafana_managed_receiver_configs", [])]
if len(configs) != 2 or any(c.get("status") != "ok" for c in configs):
    raise RuntimeError("teste Grafana de contact points falhou: " + json.dumps(result))
'''


def main():
    suffix = uuid.uuid4().hex[:12]
    mock, grafana = "cp-mock-" + suffix, "cp-grafana-" + suffix
    processes = []
    with tempfile.TemporaryFile(mode="w+") as output, tempfile.TemporaryFile(mode="w+") as grafana_log:
        try:
            processes.append(subprocess.Popen(
                ["docker", "run", "--rm", "--name", mock, "--network", "none", "--pull", "never",
                 "python:3.11-slim", "python3", "-u", "-c", MOCK],
                stdout=output, stderr=subprocess.STDOUT))
            for _ in range(30):
                result = subprocess.run(["docker", "exec", mock, "python3", "-c",
                                         "import socket; socket.create_connection(('127.0.0.1',19999),1).close()"],
                                        capture_output=True)
                if result.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("mock isolado não iniciou")
            processes.append(subprocess.Popen(
                ["docker", "run", "--rm", "--name", grafana, "--network", "container:" + mock,
                 "--pull", "never", "-e", "GF_SECURITY_ADMIN_USER=synthetic",
                 "-e", "GF_SECURITY_ADMIN_PASSWORD=synthetic-local-only",
                 "grafana/grafana:11.2.2"], stdout=grafana_log, stderr=subprocess.STDOUT))
            subprocess.run(["docker", "exec", mock, "python3", "-c", CLIENT], check=True)
            output.seek(0)
            events = [json.loads(line) for line in output if line.startswith("{")]
            if {event["path"] for event in events if event["valid"]} != {"/pagerduty", "/teams"}:
                raise RuntimeError("mocks não receberam os dois payloads esperados")
            print("PagerDuty e Teams: payloads sintéticos aceitos por mocks locais sem rede externa.")
            print("Teams enviou envelope message com Adaptive Card e TextBlock.")
        finally:
            for name in (grafana, mock):
                subprocess.run(["docker", "stop", "--time", "3", name], capture_output=True)
            for process in processes:
                process.wait(timeout=15)


if __name__ == "__main__":
    main()
