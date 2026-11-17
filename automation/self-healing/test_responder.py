import copy
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

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


if __name__ == "__main__":
    unittest.main()
