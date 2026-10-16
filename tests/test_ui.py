from __future__ import annotations

import io
import logging
import os
import unittest

from alfred.output_capture import NativeOutputCapture
from alfred.trace import MAX_EVENTS, MAX_REQUESTS, TraceStore
from alfred.ui import TerminalUI


class TraceStoreTests(unittest.TestCase):
    def test_text_input_records_structural_branches_without_input_text(self):
        trace = TraceStore()
        trace.start("text")
        trace.record("System1 · Needle", "complete", "Selected direct")
        requests, active = trace.snapshot()
        self.assertEqual(active, requests[0].number)
        self.assertEqual(requests[0].events[1].status, "skipped")
        self.assertNotIn("private request", repr(requests[0].events))

    def test_request_and_event_history_are_bounded(self):
        trace = TraceStore()
        for _ in range(MAX_REQUESTS + 1):
            trace.start("voice")
            for _ in range(MAX_EVENTS + 4):
                trace.record("Tool", "complete")
            trace.finish()
        requests, _ = trace.snapshot()
        self.assertEqual(len(requests), MAX_REQUESTS)
        self.assertEqual(requests[-1].dropped, 5)
        self.assertEqual(len(requests[-1].events), MAX_EVENTS)

    def test_control_characters_are_removed_from_trace_details(self):
        trace = TraceStore()
        trace.start("voice")
        trace.record("System1", "complete", "safe\x1b[31m\nline")
        detail = trace.snapshot()[0][-1].events[-1].detail
        self.assertNotIn("\x1b", detail)
        self.assertNotIn("\n", detail)


class ConversationUiTests(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.ui = TerminalUI(stdout=self.out, stderr=self.err)

    def tearDown(self):
        self.ui.close()

    def test_structured_tool_result_becomes_readable_message_and_speech(self):
        spoken = self.ui.present_reply(
            '{"message":"GPU status","results":[{"gpu_temp_c":57,'
            '"available":true}]}'
        )
        self.assertEqual(self.out.getvalue(),
                         "Alfred: GPU status\nGPU temperature: 57 °C\nAvailable: yes\n")
        self.assertEqual(spoken, "GPU status GPU temperature: 57 °C Available: yes")

    def test_multiline_unicode_is_kept_and_terminal_controls_are_removed(self):
        self.ui.reply("तापमान\n\x1b[31m54°C\x1b[0m")
        value = self.out.getvalue()
        self.assertIn("तापमान\n54°C", value)
        self.assertNotIn("\x1b", value)

    def test_normal_ui_never_starts_resource_sampling(self):
        calls = []
        ui = TerminalUI(stdout=io.StringIO(), stderr=io.StringIO(),
                        sampler=lambda: calls.append(True) or {})
        ui.start()
        ui.close()
        self.assertEqual(calls, [])


class NativeOutputCaptureTests(unittest.TestCase):
    def test_native_stdout_and_stderr_are_relayed_to_debug_logging(self):
        captured = []

        class Sink(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        logger = logging.getLogger("alfred.native")
        handler = Sink()
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            with NativeOutputCapture(debug=True):
                os.write(1, b"native stdout marker\n")
                os.write(2, b"native stderr marker\n")
            self.assertTrue(any("native stdout marker" in row for row in captured))
            self.assertTrue(any("native stderr marker" in row for row in captured))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)


if __name__ == "__main__":
    unittest.main()
