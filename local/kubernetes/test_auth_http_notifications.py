import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


module = load("auth_http_notifications", "configure-auth-http-notifications.py")
generator = load("auth_500_routing", "generate-auth-500.py")


class RoutingTests(unittest.TestCase):
    def test_scoped_dual_delivery_preserves_existing_routes_and_order(self):
        original = {
            "receiver": "fallback", "routes": [
                {"receiver": "local-self-healing", "continue": True,
                 "object_matchers": [["alertname", "=", generator.ALERT]]},
                {"receiver": "Discord",
                 "object_matchers": [["alertname", "=", generator.ALERT]]},
            ],
        }
        policy = module.build_policy(original)
        self.assertEqual(policy["routes"][6:], original["routes"])
        self.assertEqual(len(original["routes"]), 2)
        for uid, title in module.RULES.items():
            labels = {"alertname": title}
            if uid == "togglemaster-auth-5xx":
                labels.update(namespace="toggle", service="auth-service")
            with patch.object(generator, "LABELS", labels):
                actual = generator.receivers_for(policy)
            self.assertEqual(actual[:2], ["local-pagerduty", "Telegram"])
            if uid == "togglemaster-auth-5xx":
                self.assertEqual(actual[2:], ["local-self-healing", "Discord"])
        with patch.object(generator, "LABELS", {"alertname": "unrelated"}):
            self.assertEqual(generator.receivers_for(policy), ["fallback"])

    def test_repeated_configuration_is_idempotent(self):
        policy = module.build_policy({"receiver": "fallback"})
        self.assertEqual(module.build_policy(policy), policy)

    def test_auth_outside_namespace_does_not_notify_new_channels(self):
        policy = module.build_policy({"receiver": "fallback"})
        with patch.object(generator, "LABELS", {
                "alertname": generator.ALERT, "namespace": "other",
                "service": "auth-service"}):
            self.assertEqual(generator.receivers_for(policy), ["fallback"])


if __name__ == "__main__":
    unittest.main()
