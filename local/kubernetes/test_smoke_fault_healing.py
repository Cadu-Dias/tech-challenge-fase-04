#!/usr/bin/env python3
"""Verify that Discord routing cannot trigger a fault without an explicit opt-in."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "smoke_fault_healing", Path(__file__).with_name("smoke-fault-healing.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
validate_notification_policy = module.validate_notification_policy


MATCHERS = [
    ["alertname", "=", "Auth com taxa elevada de erros 5xx"],
    ["namespace", "=", "toggle"],
    ["service", "=", "auth-service"],
]


def route(receiver, continue_=False):
    return {"receiver": receiver, "object_matchers": MATCHERS, "continue": continue_}


class NotificationPolicyTests(unittest.TestCase):
    def test_local_only_route_remains_available(self):
        policy = {"receiver": "grafana-default-email", "routes": [route("local-self-healing")]}
        validate_notification_policy(policy, allow_discord=False)

    def test_discord_route_requires_explicit_opt_in(self):
        policy = {
            "receiver": "grafana-default-email",
            "routes": [route("local-self-healing", True), route("Discord")],
        }
        with self.assertRaisesRegex(RuntimeError, "ALLOW_DISCORD_FAULT_ALERTS=true"):
            validate_notification_policy(policy, allow_discord=False)
        validate_notification_policy(policy, allow_discord=True)

    def test_unscoped_discord_route_is_rejected(self):
        policy = {
            "receiver": "grafana-default-email",
            "routes": [route("local-self-healing", True), route("Discord")],
        }
        policy["routes"][1]["object_matchers"] = []
        with self.assertRaisesRegex(RuntimeError, "isolamento"):
            validate_notification_policy(policy, allow_discord=True)


if __name__ == "__main__":
    unittest.main()
