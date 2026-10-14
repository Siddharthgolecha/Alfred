"""Regression checks for user-visible Whistle streaming and GPU-specific tool selection."""
from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from alfred.console import TranscriptDisplay
from alfred.config import load_config
from alfred.routing import explicit_gpu_status_request, adjust_status_calls


class StatusRoutingTests(unittest.TestCase):
    def test_gpu_query_cannot_be_downgraded_to_system_status(self):
        wrong=[{"name":"get_system_status","arguments":{}}]
        correct=adjust_status_calls("What's the GPU status?",wrong)
        self.assertEqual([c["name"] for c in correct],["get_gpu_status"])
        self.assertEqual(wrong[0]["name"],"get_system_status")

    def test_gpu_temperature_and_utilization(self):
        for command in ["Show GPU temperature","GPU usage please","graphics card health",
                        "What's the GPU memory?", "GPU utilization"]:
            with self.subTest(command=command):
                self.assertTrue(explicit_gpu_status_request(command))
                result=adjust_status_calls(command,[{"name":"get_system_status","arguments":{}}])
                self.assertEqual(result[0]["name"],"get_gpu_status")

    def test_system_and_gpu_request_keeps_both(self):
        calls=[{"name":"get_system_status","arguments":{}}]
        result=adjust_status_calls("Get system and GPU status",calls)
        self.assertEqual([c["name"] for c in result],
                         ["get_system_status","get_gpu_status"])

    def test_direct_status_when_needle_returns_empty(self):
        self.assertEqual(adjust_status_calls("GPU status",[]),
                         [{"name":"get_gpu_status","arguments":{}}])

    def test_dont_touch_non_status_commands(self):
        for spoken in [
            "Remind me to check GPU status",
            "Don't show the GPU status",
            "Explain GPU utilization",
            "Show system status",
        ]:
            with self.subTest(spoken=spoken):
                calls=[{"name":"create_reminder","arguments":{
                    "message":"check GPU status","delay_minutes":20}}]
                self.assertIs(adjust_status_calls(spoken,calls),calls)

    def test_gpu_query_does_not_override_side_effects(self):
        calls=[{"name":"control_service","arguments":{"name":"monitor.service","action":"stop"}}]
        self.assertIs(adjust_status_calls("GPU status and stop monitoring",calls),calls)

    def test_only_gpu_deduplicated(self):
        calls=[{"name":"get_system_status","arguments":{}},
               {"name":"get_gpu_status","arguments":{}}]
        result=adjust_status_calls("GPU status",calls)
        self.assertEqual([c["name"] for c in result],["get_gpu_status"])


class TranscriptConsoleTests(unittest.TestCase):
    def test_progress_then_final_for_redirected_stdout(self):
        target=io.StringIO()
        display=TranscriptDisplay(target)
        display.update("GPU")
        display.update("GPU status")
        display.update("GPU status") # duplicate suppressed
        display.finish("GPU status")
        self.assertEqual(target.getvalue().splitlines(),["Whistle: GPU status"])

    def test_interactive_terminal_types_deltas_not_full_redraws(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        target=Terminal()
        display=TranscriptDisplay(target)
        display.update("show")
        display.update("show GPU")
        display.update("show GPU status")
        display.finish("show GPU status")
        self.assertEqual(target.getvalue(),"Whistle: show GPU status\n")

    def test_interactive_terminal_handles_revised_hypothesis(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        target=Terminal()
        display=TranscriptDisplay(target)
        display.update("show CPO")
        display.update("show GPU")
        display.finish("show GPU")
        output=target.getvalue()
        self.assertEqual(output.count("Whistle:"),1)
        self.assertIn("\b",output)
        self.assertEqual(output.count("\n"),1)

    def test_control_bytes_cannot_inject_terminal_escapes(self):
        target=io.StringIO()
        display=TranscriptDisplay(target)
        display.update("\x1b[31m\ndelete\x07")
        display.finish("\x1b[31mGPU\x1b[0m")
        self.assertNotIn("\x1b",target.getvalue())
        self.assertNotIn("\x07",target.getvalue())
        self.assertIn("Whistle: GPU",target.getvalue())

    def test_no_partial_execution(self):
        from alfred.app import Alfred
        cfg=load_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["runtime"]["database"]=str(Path(tmp)/"db.sqlite")
            cfg["runtime"]["logs"]=tmp
            app=Alfred(cfg,load_models=False)
            app.cactus=Mock()
            called=[]
            def simulate(chunks,on_partial):
                on_partial("Start unsafe service")
                on_partial("GPU sta")
                return "GPU status"
            app.cactus.transcribe_stream.side_effect=simulate
            app.handle_text=Mock(side_effect=lambda text:called.append(text))
            class OneUtterance:
                def __init__(self,*args,**kwargs):
                    pass
                def utterances(self):
                    return iter([[[]]])
            output=io.StringIO()
            try:
                with patch("alfred.app.UtteranceStream",OneUtterance),patch("sys.stdout",output):
                    app.listen()
                self.assertEqual(called,["GPU status"])
                self.assertNotIn("unsafe service",output.getvalue())
                self.assertEqual(output.getvalue(),"Whistle: GPU status\n")
            finally:
                app.close()
                app.store.db.close()

    def test_cli_status_routing_uses_gpu_tool(self):
        from alfred.app import Alfred
        cfg=load_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["runtime"]["database"]=str(Path(tmp)/"db.sqlite")
            cfg["runtime"]["logs"]=tmp
            app=Alfred(cfg,load_models=False)
            app.cactus=Mock()
            app.cactus.route.return_value={
                "confidence":0.99,
                "function_calls":[{"name":"get_system_status","arguments":{}}]
            }
            app.system2.direct=Mock(return_value={"message":"OK","results":[]})
            app.feedback.tell=Mock()
            try:
                app.handle_text("What's the GPU status?")
                selected=app.system2.direct.call_args.args[0]
                self.assertEqual([c["name"] for c in selected],["get_gpu_status"])
            finally:
                app.close()
                app.store.db.close()


if __name__=="__main__":
    unittest.main()
