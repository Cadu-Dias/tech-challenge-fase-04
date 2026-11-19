#!/usr/bin/env python3
"""Exercise the Datadog activation gate without touching Kubernetes."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ActivationGateTest(unittest.TestCase):
    def run_gate(self, failing_check):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            log = directory / "calls"
            for name, body in {
                "kubectl": """#!/bin/sh
printf '%s\\n' "$*" >> "$CALLS"
case "$*" in
  *"config view"*) echo https://127.0.0.1:1234 ;;
  *"create configmap"*) echo synthetic ;;
esac
""",
                "python3": """#!/bin/sh
printf '%s\\n' "$1" >> "$CALLS"
if [ "$1" = "$FAIL_ON" ]; then exit 1; fi
""",
            }.items():
                command = directory / name
                command.write_text(body)
                command.chmod(0o700)
            environment = dict(os.environ, KUBECONFIG="synthetic", CALLS=str(log),
                               FAIL_ON=failing_check,
                               PATH=str(directory) + os.pathsep + os.environ["PATH"])
            result = subprocess.run(
                ["sh", str(ROOT / "local/kubernetes/enable-datadog.sh")],
                cwd=ROOT, env=environment, capture_output=True, text=True,
            )
            return result.returncode, log.read_text()

    def test_pre_activation_failure_disables_existing_pipeline(self):
        status, calls = self.run_gate("local/kubernetes/test-sanitize.py")
        self.assertNotEqual(status, 0)
        self.assertIn("datadog-disable-patch.yaml", calls)
        self.assertNotIn("datadog-patch.yaml", calls)

    def test_post_activation_failure_disables_pipeline(self):
        status, calls = self.run_gate("local/kubernetes/smoke-datadog.py")
        self.assertNotEqual(status, 0)
        self.assertIn("datadog-patch.yaml", calls)
        self.assertIn("datadog-disable-patch.yaml", calls)

    def test_success_keeps_pipeline_enabled(self):
        status, calls = self.run_gate("")
        self.assertEqual(status, 0)
        self.assertIn("datadog-patch.yaml", calls)
        self.assertNotIn("datadog-disable-patch.yaml", calls)


if __name__ == "__main__":
    unittest.main()
