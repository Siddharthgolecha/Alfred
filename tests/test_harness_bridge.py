"""Offline tests for the unprivileged harness event boundary."""
import json
import unittest
from alfred.cognitive.harness_bridge import (
    HarnessBridge, HarnessConfig, HarnessProtocolError, decode_frame, encode_frame,
)


def frame(payload=None, event="observation"):
    return json.dumps({"version": 1, "event": event,
                       "payload": {"tool": "status", "outcome": "returned"}
                       if payload is None else payload}).encode()


class HarnessBoundaryTests(unittest.TestCase):
    def test_disabled_by_default(self):
        with self.assertRaises(HarnessProtocolError):
            HarnessBridge().receive(frame())

    def test_safe_observation(self):
        bridge = HarnessBridge(HarnessConfig(enabled=True))
        self.assertEqual(bridge.receive(frame())["payload"]["tool"], "status")

    def test_fake_roundtrip(self):
        bridge = HarnessBridge(HarnessConfig(enabled=True))
        result = bridge.send("task", {"session_id": "a" * 32, "task_id": "b" * 32,
                                     "source": "text", "input_chars": 5,
                                     "started_at": 123},
                             lambda request: encode_frame("observation",
                                                          {"tool": "status", "outcome": "returned"}))
        self.assertEqual(result["event"], "observation")

    def test_no_unsafe_capabilities(self):
        for field in ("allow_shell", "allow_network", "allow_plugins"):
            with self.subTest(field=field), self.assertRaises(HarnessProtocolError):
                HarnessBridge(HarnessConfig(**{field: True}))
        with self.assertRaises(HarnessProtocolError):
            HarnessBridge(HarnessConfig(offline=False))

    def test_forbidden_proposals(self):
        for key in ("execute", "shell", "plugin", "url", "approval", "command",
                    "arguments", "network", "tool_calls"):
            with self.subTest(key=key), self.assertRaises(HarnessProtocolError):
                decode_frame(frame({"tool": "status", "outcome": "returned", key: "anything"}))

    def test_bounded_and_strict(self):
        for raw in (b"", b"x" * 8193,
                    b'{"version":1,"version":1,"event":"task","payload":{}}',
                    b'{"version":true,"event":"task","payload":{}}',
                    b'{"version":1,"event":"unknown","payload":{}}',
                    b'{"version":1,"event":"observation","payload":{"tool":"x","outcome":NaN}}',
                    frame({"tool": "status", "outcome": "returned", "extra": "x"}),
                    frame({"tool": "status", "outcome": "returned", "nested": {"x": [1]}})):
            with self.subTest(raw=raw[:30]), self.assertRaises(HarnessProtocolError):
                decode_frame(raw)

    def test_canonical_projection_and_enum_validation(self):
        from alfred.cognitive.contracts import TaskEnvelope, Decision
        from alfred.cognitive.harness_bridge import frame_for_contract
        task = TaskEnvelope("a" * 32, "b" * 32, "voice", 3, 123)
        self.assertEqual(decode_frame(frame_for_contract(task))["event"], "task")
        self.assertEqual(decode_frame(frame_for_contract(Decision(
            "needle3", "direct", 1, .8)))["event"], "decision")
        with self.assertRaises(HarnessProtocolError):
            frame_for_contract({"tool":"arbitrary-shell", "args":{}})
        for invalid in ({"engine": "evil", "route": "direct", "call_count": 1, "confidence": 0.3},
                        {"engine": "needle3", "route": "direct", "call_count": True, "confidence": None}):
            with self.subTest(invalid=invalid), self.assertRaises(HarnessProtocolError):
                encode_frame("decision", invalid)

    def test_invalid_task_ids(self):
        with self.assertRaises(HarnessProtocolError):
            encode_frame("task", {"session_id": "bad", "task_id": "b" * 32,
                                  "source": "text", "input_chars": 2})


if __name__ == "__main__":
    unittest.main()
