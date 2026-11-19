import copy
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import responder


def alert(status="firing", fingerprint="a" * 16):
    return {
        "status": status, "fingerprint": fingerprint, "startsAt": "2026-11-17T00:00:00Z",
        "labels": {"alertname": responder.ALERT, "service": "auth-service", "namespace": "toggle"},
    }


class FakeAPI:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"metadata": {"resourceVersion": "1"}, "data": {"state": "{}"}}
        self.deployment = {
            "metadata": {"resourceVersion": "1", "generation": 1},
            "spec": {"replicas": 2, "template": {"metadata": {"annotations": {}}}},
            "status": {"observedGeneration": 1, "updatedReplicas": 2, "availableReplicas": 2, "replicas": 2},
        }
        self.patches = 0
        self.patch_error = False

    def get_state(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def put_state(self, obj):
        with self.lock:
            if obj["metadata"]["resourceVersion"] != self.state["metadata"]["resourceVersion"]:
                raise responder.APIError(409, "conflict")
            obj["metadata"]["resourceVersion"] = str(int(obj["metadata"]["resourceVersion"]) + 1)
            self.state = copy.deepcopy(obj)

    def get_deployment(self):
        with self.lock:
            return copy.deepcopy(self.deployment)

    def patch_deployment(self, obj):
        with self.lock:
            self.patches += 1
            if self.patch_error:
                raise responder.APIError(403, "denied")
            self.deployment["spec"]["template"]["metadata"]["annotations"].update(
                obj["spec"]["template"]["metadata"]["annotations"]
            )
            self.deployment["metadata"]["generation"] += 1
            self.deployment["status"]["observedGeneration"] += 1
            return copy.deepcopy(self.deployment)


class ResponderTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.time = 10000
        self.worker = responder.Responder(self.api, now=lambda: self.time, sleep=lambda _: None)

    def test_restart_once_and_persist_across_instance(self):
        self.assertEqual(self.worker.process(alert()), "restarted")
        self.assertEqual(responder.Responder(self.api, now=lambda: self.time).process(alert()), "duplicate")
        self.assertEqual(self.api.patches, 1)

    def test_resolved_wrong_target_and_cooldown_do_not_patch(self):
        self.assertEqual(self.worker.process(alert("resolved")), "ignored")
        bad = alert()
        bad["labels"]["namespace"] = "other"
        with self.assertRaises(ValueError):
            self.worker.process(bad)
        self.assertEqual(self.worker.process(alert()), "restarted")
        self.assertEqual(self.worker.process(alert(fingerprint="b" * 16)), "cooldown")
        self.assertEqual(self.api.patches, 1)

    def test_failure_is_recorded_and_not_retried(self):
        self.api.patch_error = True
        with self.assertRaises(responder.APIError):
            self.worker.process(alert())
        with self.assertRaises(responder.APIError):
            self.worker.process(alert())
        self.assertEqual(self.api.patches, 1)

    def test_concurrent_same_incident_never_duplicates_patch(self):
        results = []
        barrier = threading.Barrier(8)

        def run():
            barrier.wait()
            try:
                results.append(self.worker.process(alert()))
            except responder.APIError:
                results.append("inflight")

        threads = [threading.Thread(target=run) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.api.patches, 1)
        self.assertEqual(results.count("restarted"), 1)

    def test_inflight_other_incident_is_blocked_even_after_cooldown(self):
        started = threading.Event()
        release = threading.Event()
        original_patch = self.api.patch_deployment
        results = []

        def blocked_patch(obj):
            started.set()
            if not release.wait(5):
                raise responder.APIError(503, "test timed out")
            return original_patch(obj)

        self.api.patch_deployment = blocked_patch
        thread = threading.Thread(target=lambda: results.append(self.worker.process(alert())))
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            self.time += responder.COOLDOWN_SECONDS + 1
            with self.assertRaises(responder.APIError) as raised:
                self.worker.process(alert(fingerprint="b" * 16))
            self.assertEqual(raised.exception.status, 409)
            self.assertEqual(self.api.patches, 0)
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, ["restarted"])
        self.assertEqual(self.worker.process(alert(fingerprint="b" * 16)), "restarted")
        self.assertEqual(self.api.patches, 2)

    def test_orphaned_reservation_blocks_other_incidents(self):
        self.api.state["data"]["state"] = json.dumps({
            "incidents": {"previous": "reserved"}, "last_restart": 1,
        })
        with self.assertRaises(responder.APIError):
            self.worker.process(alert())
        self.assertEqual(self.api.patches, 0)

    def test_invalid_state_fails_closed(self):
        for state in (
            {"incidents": {"x": "unknown"}},
            {"incidents": {}, "last_restart": float("nan")},
            {"incidents": {}, "last_restart": True},
        ):
            with self.subTest(state=state):
                self.api.state["data"]["state"] = json.dumps(state)
                with self.assertRaises(responder.APIError):
                    self.worker.process(alert())
        self.assertEqual(self.api.patches, 0)

    def test_initial_incident_at_epoch_is_not_in_cooldown(self):
        self.time = 0
        self.assertEqual(self.worker.process(alert()), "restarted")

    def test_http_auth_and_invalid_batch_cannot_patch(self):
        token = "synthetic-token-" + "x" * 32
        server = ThreadingHTTPServer(("127.0.0.1", 0), responder.handler(self.worker, token))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/webhook"

        def post(body, bearer):
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": bearer, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                with error:
                    return error.code

        self.assertEqual(post({"alerts": [alert()]}, "Bearer wrong"), 401)
        self.assertEqual(post({"alerts": [alert(), {"status": "firing"}]}, "Bearer " + token), 400)
        self.assertEqual(self.api.patches, 0)
        self.api.state["data"]["state"] = json.dumps({"incidents": {"other": "reserved"}})
        self.assertEqual(post({"alerts": [alert()]}, "Bearer " + token), 503)
        self.assertEqual(self.api.patches, 0)
        self.api.state["data"]["state"] = "{}"
        self.assertEqual(post({"alerts": [alert()]}, "Bearer " + token), 200)
        self.assertEqual(self.api.patches, 1)

    def test_rollout_timeout_remains_failed(self):
        clock = [10000]
        self.api.deployment["status"]["availableReplicas"] = 1
        worker = responder.Responder(
            self.api, now=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds), rollout_timeout=4,
        )
        with self.assertRaises(responder.APIError) as raised:
            worker.process(alert())
        self.assertEqual(raised.exception.status, 504)
        with self.assertRaises(responder.APIError):
            worker.process(alert())
        self.assertEqual(self.api.patches, 1)

    def test_kubernetes_transport_and_invalid_json_fail_explicitly(self):
        api = object.__new__(responder.Kubernetes)
        api.base = "https://127.0.0.1"
        api.token = "synthetic"
        api.ssl = None
        with patch("responder.urllib.request.urlopen", side_effect=TimeoutError):
            with self.assertRaises(responder.APIError) as raised:
                api.get_state()
            self.assertEqual(raised.exception.status, 503)
        with patch("responder.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b"not-json"
            with self.assertRaises(responder.APIError) as raised:
                api.get_state()
            self.assertEqual(raised.exception.status, 502)


if __name__ == "__main__":
    unittest.main()
