#!/usr/bin/env python3
"""Rotate local synthetic TLS certificates without printing credentials."""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile

import smoke


def command(*args, **kwargs):
    return subprocess.run(smoke.KUBECTL + list(args), check=True, **kwargs)


def replace_certificate(namespace, name, certificate, private_key=None):
    result = command("-n", namespace, "get", "secret", name, "-o", "json",
                     capture_output=True, text=True)
    secret = json.loads(result.stdout)
    secret["data"]["tls.crt"] = base64.b64encode(certificate).decode()
    if private_key is not None:
        secret["data"]["tls.key"] = base64.b64encode(private_key).decode()
    command("replace", "-f", "-", input=json.dumps(secret), text=True, capture_output=True)


def generate(directory, name, hostname, extras=""):
    key, cert = directory / (name + ".key"), directory / (name + ".crt")
    san = "subjectAltName=DNS:" + hostname + extras
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "7",
                    "-keyout", str(key), "-out", str(cert), "-subj", "/CN=" + hostname,
                    "-addext", san], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["openssl", "verify", "-CAfile", str(cert), str(cert)], check=True,
                   stdout=subprocess.DEVNULL)
    return cert.read_bytes(), key.read_bytes()


def restart(namespace, resource):
    command("-n", namespace, "rollout", "restart", resource, capture_output=True)
    command("-n", namespace, "rollout", "status", resource, "--timeout=150s", capture_output=True)


def main():
    if not os.environ.get("KUBECONFIG"):
        raise RuntimeError("KUBECONFIG local obrigatório")
    server = command("config", "view", "--minify", "-o",
                     "jsonpath={.clusters[0].cluster.server}", capture_output=True, text=True).stdout
    if not server.startswith("https://127.0.0.1:"):
        raise RuntimeError("API Kubernetes não está no loopback")
    for namespace, name, keys in (
        ("toggle", "telemetry-auth", {"tls.crt", "tls.key"}),
        ("observability", "telemetry-agent-auth", {"tls.crt"}),
        ("observability", "telemetry-gateway-auth", {"tls.crt", "tls.key"}),
        ("toggle", "toggle-self-healing-tls", {"tls.crt", "tls.key"}),
        ("observability", "self-healing-client", {"tls.crt"}),
    ):
        result = command("-n", namespace, "get", "secret", name, "-o", "json",
                         capture_output=True, text=True)
        if not keys <= json.loads(result.stdout).get("data", {}).keys():
            raise RuntimeError("Secret local sem chaves de certificado esperadas: " + name)
    for namespace, resource in (
        ("observability", "deployment/otel-collector"),
        ("observability", "daemonset/otel-log-agent"),
        ("observability", "deployment/grafana"),
        ("toggle", "deployment/toggle-self-healing"),
        *(("toggle", "deployment/" + service + "-service")
          for service in ("auth", "flag", "targeting", "evaluation", "analytics")),
    ):
        command("-n", namespace, "get", resource, "-o", "name", capture_output=True)
    args = command("-n", "observability", "get", "deployment", "otel-collector", "-o",
                   "jsonpath={.spec.template.spec.containers[0].args}", capture_output=True, text=True).stdout
    datadog_enabled = "datadog-pipeline.yaml" in args
    root = Path(__file__).resolve().parents[2]
    if datadog_enabled:
        subprocess.run(["sh", str(root / "local/kubernetes/disable-datadog.sh")], check=True)
    with tempfile.TemporaryDirectory(prefix="toggle-cert-") as temp:
        directory = Path(temp)
        telemetry_cert, telemetry_key = generate(
            directory, "telemetry", "otel-collector.observability.svc.cluster.local", ",DNS:otel-collector")
        webhook_cert, webhook_key = generate(
            directory, "webhook", "toggle-self-healing.toggle.svc.cluster.local", ",IP:127.0.0.1")
        replace_certificate("toggle", "telemetry-auth", telemetry_cert, telemetry_key)
        replace_certificate("observability", "telemetry-agent-auth", telemetry_cert)
        replace_certificate("observability", "telemetry-gateway-auth", telemetry_cert, telemetry_key)
        replace_certificate("toggle", "toggle-self-healing-tls", webhook_cert, webhook_key)
        replace_certificate("observability", "self-healing-client", webhook_cert)
    restart("observability", "deployment/otel-collector")
    restart("observability", "daemonset/otel-log-agent")
    for service in ("auth", "flag", "targeting", "evaluation", "analytics"):
        restart("toggle", "deployment/" + service + "-service")
    restart("toggle", "deployment/toggle-self-healing")
    restart("observability", "deployment/grafana")
    for check in ("smoke-otlp-auth.py", "smoke-auth-metrics.py", "smoke-webhook-tls.py"):
        subprocess.run(["python3", str(root / "local/kubernetes" / check)], check=True)
    if datadog_enabled:
        subprocess.run(["sh", str(root / "local/kubernetes/enable-datadog.sh")], check=True)
    print("Certificados locais renovados; componentes afetados reiniciados.")


if __name__ == "__main__":
    main()
