import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error


spec = importlib.util.spec_from_file_location(
    "generate_auth_500", Path(__file__).with_name("generate-auth-500.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def route(receiver, continue_=False):
    return {"receiver": receiver, "continue": continue_,
            "object_matchers": [[k, "=", v] for k, v in module.LABELS.items()]}


class GeneratorTests(unittest.TestCase):
    def test_pagerduty_must_be_reachable_not_just_present(self):
        policy = {"receiver": "fallback", "routes": [
            route("Discord"), route("local-pagerduty"),
        ]}
        self.assertEqual(module.receivers_for(policy), ["Discord"])
        policy["routes"].insert(0, route("local-pagerduty", True))
        self.assertEqual(module.receivers_for(policy), ["local-pagerduty", "Discord"])

    def test_unrelated_route_and_nested_inheritance(self):
        policy = {"receiver": "fallback", "routes": [
            {"receiver": "other", "object_matchers": [["service", "=", "other"]]},
            {"receiver": "local-pagerduty", "routes": [
                {"object_matchers": [["namespace", "=", "toggle"]]},
            ]},
        ]}
        self.assertEqual(module.receivers_for(policy), ["local-pagerduty"])

    def test_no_generation_by_default_and_explicit_gate(self):
        with patch("sys.argv", ["script"]):
            self.assertFalse(module.arguments().run)
        with patch("sys.argv", ["script", "--run"]), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                module.arguments()
        for value in ("0", "901"):
            with patch("sys.argv", ["script", "--duration", value]), \
                    patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    module.arguments()

    def test_500_counted_and_deadline_stops_generation(self):
        error = urllib.error.HTTPError("http://127.0.0.1", 500, "error", {}, None)
        with patch.object(module, "request", side_effect=error) as request, \
                patch.object(module.time, "monotonic", side_effect=[0, 0, 1, 120]), \
                patch.object(module.time, "sleep"), patch("sys.stdout", io.StringIO()):
            self.assertEqual(module.generate("http://127.0.0.1", "secret", 120, 1), 1)
        self.assertEqual(request.call_count, 1)

    def test_unexpected_status_stops_without_retry(self):
        for response in (None, urllib.error.HTTPError(
                "http://127.0.0.1", 401, "unauthorized", {}, None)):
            with patch.object(module, "request", side_effect=response) as request:
                with self.assertRaises(RuntimeError):
                    module.generate("http://127.0.0.1", "secret", 120, 1)
                self.assertEqual(request.call_count, 1)

    def test_pod_restart_reconnects_without_resetting_deadline(self):
        error = urllib.error.HTTPError("http://127.0.0.1", 500, "error", {}, None)
        with patch.object(module, "request", side_effect=[
                urllib.error.URLError("disconnected"), error]) as request, \
                patch.object(module.time, "monotonic",
                             side_effect=[0, 0, 1, 2, 3, 4, 120]), \
                patch.object(module.time, "sleep"), patch("sys.stdout", io.StringIO()):
            reconnect = unittest.mock.Mock(return_value="http://127.0.0.1:1234")
            self.assertEqual(module.generate(
                "http://127.0.0.1", "secret", 120, 1, reconnect,
            ), 1)
            reconnect.assert_called_once()
            self.assertEqual(request.call_args.args[0], "http://127.0.0.1:1234")

    def test_connection_failure_retries_are_bounded(self):
        with patch.object(module, "request", side_effect=urllib.error.URLError("down")), \
                patch.object(module.time, "sleep"), patch("sys.stdout", io.StringIO()):
            reconnect = unittest.mock.Mock(return_value="http://127.0.0.1")
            with self.assertRaisesRegex(RuntimeError, "tentativas limitadas"):
                module.generate("http://127.0.0.1", "secret", 120, 1, reconnect)
            self.assertEqual(reconnect.call_count, 3)


if __name__ == "__main__":
    unittest.main()
