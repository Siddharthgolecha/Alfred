"""Negative tests for the optional harness protocol boundary."""
import json
import unittest

from alfred.cognitive.harness_bridge import (
    HarnessBridge, HarnessConfig, HarnessProtocolError, decode_frame,
)


def frame(payload=None):
    return json.dumps({"version": 1, "event": "observation",
                       "payload": payload or {}}).encode()


class HarnessBoundaryTests(unittest.TestCase):
    def test_disabled_by_default(self):
        with self.assertRaises(HarnessProtocolError):
            HarnessBridge().receive(frame())

    def test_safe_observation(self):
        self.assertEqual(HarnessBridge(HarnessConfig(enabled=True)).receive(
            frame({"outcome": "returned"}))["event"], "observation")

    def test_no_unsafe_capabilities(self):
        for field in ("allow_shell", "allow_network", "allow_plugins"):
            with self.subTest(field=field), self.assertRaises(HarnessProtocolError):
                HarnessBridge(HarnessConfig(**{field: True}))
        with self.assertRaises(HarnessProtocolError):
            HarnessBridge(HarnessConfig(offline=False))

    def test_forbidden_proposals(self):
        for key in ("execute", "shell", "plugin", "url", "approval"):
            with self.subTest(key=key), self.assertRaises(HarnessProtocolError):
                decode_frame(frame({key: "anything"}))

    def test_bounded_and_strict(self):
        for raw in (b"", b"x" * 8193, b'{"version":1,"version":1,"event":"task","payload":{}}',
                    b'{"version":true,"event":"task","payload":{}}',
                    b'{"version":1,"event":"unknown","payload":{}}'):
            with self.subTest(raw=raw[:30]), self.assertRaises(HarnessProtocolError):
                decode_frame(raw)


if __name__ == "__main__":
    unittest.main()
