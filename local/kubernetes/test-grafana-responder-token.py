#!/usr/bin/env python3
import base64
import importlib.util
from pathlib import Path
import unittest


path = Path(__file__).with_name("setup-grafana-responder.py")
spec = importlib.util.spec_from_file_location("setup_grafana_responder", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TokenTest(unittest.TestCase):
    def test_removes_only_outer_whitespace(self):
        value = b"a" * 64
        self.assertEqual(module.normalize_token(base64.b64encode(value + b"\n")), value)

    def test_rejects_internal_newline(self):
        with self.assertRaises(ValueError):
            module.normalize_token(base64.b64encode(b"a" * 32 + b"\n" + b"b" * 32))

    def test_rejects_short_token(self):
        with self.assertRaises(ValueError):
            module.normalize_token(base64.b64encode(b"short"))


if __name__ == "__main__":
    unittest.main()
