import hashlib
import hmac
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NAMESPACE = "toggle"
DEPLOYMENT = "auth-service"
STATE = "toggle-self-healing-state"
ALERT = "Auth com taxa elevada de erros 5xx"
COOLDOWN_SECONDS = 600
MAX_RECORDS = 500
ANNOTATION = "togglemaster.io/restarted-at"


class APIError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Kubernetes:
    def __init__(self):
        self.base = "https://" + os.environ["KUBERNETES_SERVICE_HOST"] + ":" + os.environ["KUBERNETES_SERVICE_PORT"]
        service_account = "/var/run/secrets/kubernetes.io/serviceaccount"
        with open(service_account + "/token", encoding="utf-8") as file:
            self.token = file.read().strip()
        self.ssl = ssl.create_default_context(cafile=service_account + "/ca.crt")

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Authorization": "Bearer " + self.token}
        if body is not None:
            headers["Content-Type"] = "application/merge-patch+json" if method == "PATCH" else "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=self.ssl, timeout=5) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise APIError(error.code, "Kubernetes API rejected request") from None
        except urllib.error.URLError:
            raise APIError(503, "Kubernetes API unavailable") from None

    def get_state(self):
        return self.call("GET", f"/api/v1/namespaces/{NAMESPACE}/configmaps/{STATE}")

    def put_state(self, obj):
        return self.call("PUT", f"/api/v1/namespaces/{NAMESPACE}/configmaps/{STATE}", obj)

    def get_deployment(self):
        return self.call("GET", f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{DEPLOYMENT}")

    def patch_deployment(self, obj):
        return self.call("PATCH", f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{DEPLOYMENT}", obj)


class Responder:
    def __init__(self, api, now=time.time, sleep=time.sleep, rollout_timeout=120):
        self.api = api
        self.now = now
        self.sleep = sleep
        self.rollout_timeout = rollout_timeout

    def update_state(self, mutate):
        for _ in range(5):
            obj = self.api.get_state()
            try:
                state = json.loads(obj.get("data", {}).get("state", "{}"))
            except json.JSONDecodeError:
                raise APIError(500, "invalid state") from None
            if not isinstance(state, dict):
                raise APIError(500, "invalid state")
            result = mutate(state)
            if result is not None:
                return result
            obj.setdefault("data", {})["state"] = json.dumps(state, separators=(",", ":"), sort_keys=True)
            try:
                self.api.put_state(obj)
                return None
            except APIError as error:
                if error.status != 409:
                    raise
        raise APIError(409, "state contention")

    def validate(self, alert):
        if not isinstance(alert, dict):
            raise ValueError("invalid alert")
        if alert.get("status") == "resolved":
            return None
        if alert.get("status") != "firing":
            raise ValueError("invalid status")
        labels = alert.get("labels")
        if not isinstance(labels, dict) or labels.get("alertname") != ALERT or \
                labels.get("service") != DEPLOYMENT or labels.get("namespace") != NAMESPACE:
            raise ValueError("alert not allowed")
        fingerprint = alert.get("fingerprint")
        starts_at = alert.get("startsAt")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{16,64}", fingerprint):
            raise ValueError("invalid fingerprint")
        if not isinstance(starts_at, str) or len(starts_at) > 40:
            raise ValueError("invalid start time")
        try:
            start = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
            if start.tzinfo is None:
                raise ValueError("missing timezone")
        except ValueError:
            raise ValueError("invalid start time") from None
        return hashlib.sha256((fingerprint + "\n" + start.astimezone(timezone.utc).isoformat()).encode()).hexdigest()

    def process(self, alert):
        incident = self.validate(alert)
        if incident is None:
            return "ignored"
        timestamp = self.now()

        def reserve(state):
            records = state.setdefault("incidents", {})
            if not isinstance(records, dict) or len(records) > MAX_RECORDS:
                raise APIError(500, "invalid state")
            if incident in records:
                if records[incident] == "completed":
                    return "duplicate"
                raise APIError(409, "previous attempt did not complete successfully")
            if len(records) == MAX_RECORDS:
                raise APIError(507, "incident state capacity reached")
            last_restart = state.get("last_restart", 0)
            if not isinstance(last_restart, (int, float)):
                raise APIError(500, "invalid state")
            if timestamp - last_restart < COOLDOWN_SECONDS:
                return "cooldown"
            records[incident] = "reserved"
            state["last_restart"] = timestamp
            return None

        reserved = self.update_state(reserve)
        if reserved is not None:
            return reserved
        try:
            deployment = self.api.get_deployment()
            replicas = deployment.get("spec", {}).get("replicas", 1)
            if not isinstance(replicas, int) or replicas < 1:
                raise APIError(409, "deployment has no desired replicas")
            restart_id = datetime.now(timezone.utc).isoformat() + "-" + uuid.uuid4().hex
            patch = {
                "metadata": {"resourceVersion": deployment["metadata"]["resourceVersion"]},
                "spec": {"template": {"metadata": {"annotations": {
                    ANNOTATION: restart_id
                }}}},
            }
            updated = self.api.patch_deployment(patch)
            generation = updated["metadata"]["generation"]
            deadline = self.now() + self.rollout_timeout
            while self.now() < deadline:
                current = self.api.get_deployment()
                status = current.get("status", {})
                desired = current.get("spec", {}).get("replicas", 1)
                if desired != replicas:
                    raise APIError(409, "replica count changed during rollout")
                annotations = current.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations", {})
                if annotations.get(ANNOTATION) != restart_id:
                    raise APIError(409, "restart annotation changed during rollout")
                if status.get("observedGeneration", 0) >= generation and \
                        status.get("updatedReplicas", 0) == replicas and \
                        status.get("availableReplicas", 0) == replicas and \
                        status.get("replicas", 0) == replicas and \
                        status.get("unavailableReplicas", 0) == 0:
                    break
                self.sleep(2)
            else:
                raise APIError(504, "rollout timed out")
        except (APIError, KeyError, ValueError):
            self.finish(incident, "failed")
            raise
        self.finish(incident, "completed")
        return "restarted"

    def finish(self, incident, outcome):
        def update(state):
            records = state.get("incidents", {})
            if records.get(incident) != "reserved":
                raise APIError(409, "incident reservation missing")
            records[incident] = outcome
            return None
        self.update_state(update)


def handler(responder, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            pass

        def do_GET(self):
            if self.path != "/health":
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            if self.path != "/webhook":
                self.send_error(404)
                return
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()):
                self.send_error(401)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 32768:
                    raise ValueError("invalid length")
                payload = json.loads(self.rfile.read(length))
                alerts = payload.get("alerts")
                if not isinstance(alerts, list) or not 1 <= len(alerts) <= 20:
                    raise ValueError("invalid alerts")
                for alert in alerts:
                    responder.validate(alert)
                results = [responder.process(alert) for alert in alerts]
            except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
                self.send_error(400)
                return
            except APIError as error:
                print(json.dumps({"event": "self_healing.error", "status": error.status}), flush=True)
                self.send_error(503)
                return
            print(json.dumps({"event": "self_healing.result", "results": results}), flush=True)
            body = json.dumps({"results": results}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return Handler


def main():
    with open(os.environ["WEBHOOK_TOKEN_FILE"], encoding="utf-8") as file:
        token = file.read().strip()
    if len(token) < 32 or "\n" in token:
        raise ValueError("webhook token must be at least 32 characters")
    server = ThreadingHTTPServer(("0.0.0.0", 8080), handler(Responder(Kubernetes()), token))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(os.environ["WEBHOOK_TLS_CERT_FILE"], os.environ["WEBHOOK_TLS_KEY_FILE"])
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
