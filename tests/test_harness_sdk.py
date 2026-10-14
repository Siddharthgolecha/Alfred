"""Fail-closed official SDK adapter and read-only Alfred integration tests."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from alfred.config import load_config
from alfred.app import Alfred
from alfred.cognitive.harness_sdk import (
    LocalHarness, HarnessUnavailable, check_safe_patch, DENIED_ROWS, SDK_VERSION)


class FakeSdk:
    created = []
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        self.closed = False
        type(self).created.append(self)

    def run(self, prompt, session_id):
        self.calls.append((prompt, session_id))
        return Mock(final_response="LOCAL ACK", finish_reason="completed")

    def close(self):
        self.closed = True


class SafeSdkTests(unittest.TestCase):
    def setUp(self):
        FakeSdk.created.clear()

    def test_patch_is_allowlisted_disable_only(self):
        self.assertEqual(len(check_safe_patch()), 64)
        self.assertIn("persistent-bash", DENIED_ROWS)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "unsafe.yml"
            for text in (
                    "- id: persistent-bash\n  disabled: false\n",
                    "- id: unexpected-plugin\n  disabled: true\n",
                    "- insert:\n  - name: '@anywhere/tool'\n",
                    "- id: persistent-bash\n  disabled: true\n- id: persistent-bash\n  disabled: true\n"):
                p.write_text(text)
                with self.subTest(text=text), self.assertRaises(HarnessUnavailable):
                    check_safe_patch(p)

    def test_remote_urls_and_invalid_ids_fail_closed(self):
        for url in ("https://127.0.0.1:8000", "http://example.com:8000",
                    "http://evil@127.0.0.1:8000", "http://127.0.0.1:8000/unsafe",
                    "http://127.0.0.1:8000?plugin=1", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(HarnessUnavailable):
                LocalHarness(url, "qwen", factory=FakeSdk)
        client = LocalHarness("http://127.0.0.1:8080", "qwen", factory=FakeSdk)
        with self.assertRaises(HarnessUnavailable):
            client.run("hello", "not-an-id")
        with self.assertRaises(HarnessUnavailable):
            client.run("x" * 9000, "a" * 32)
        self.assertEqual(FakeSdk.created, [])
        client.close()

    def test_sdk_factory_is_real_api_shaped_and_reused(self):
        client = LocalHarness("http://127.0.0.1:8080", "qwen", factory=FakeSdk)
        try:
            self.assertEqual(client.run("safe", "a" * 32)["message"], "LOCAL ACK")
            self.assertEqual(client.run("more", "a" * 32)["message"], "LOCAL ACK")
            self.assertEqual(len(FakeSdk.created), 1)
            sdk = FakeSdk.created[0]
            self.assertEqual(len(sdk.calls), 2)
            self.assertEqual(sdk.kwargs["profile"], "sdk-minimal")
            self.assertEqual(sdk.kwargs["base_url"], "http://127.0.0.1:8080/v1")
            self.assertEqual(sdk.kwargs["patches"], (str(client.patch),))
            self.assertEqual(sdk.kwargs["shutdown_timeout_seconds"], 1)
            self.assertNotIn("shell", json.dumps(sdk.kwargs).lower())
            home = Path(sdk.kwargs["dsh_home"]).parent
            self.assertTrue(home.exists())
        finally:
            client.close()
        self.assertTrue(sdk.closed)
        self.assertFalse(home.exists())
        with self.assertRaises(HarnessUnavailable):
            client.run("after close", "a" * 32)

    def test_timeout_closes_owned_runtime(self):
        class Slow(FakeSdk):
            def run(self, *args, **kwargs):
                time.sleep(6)
                return super().run(*args, **kwargs)
        client = LocalHarness("http://localhost:8080", "qwen", timeout=5, factory=Slow)
        with self.assertRaises(HarnessUnavailable):
            client.run("hi", "a" * 32)
        self.assertTrue(Slow.created[0].closed)
        self.assertIsNone(client._sdk)

    def test_incomplete_response_cannot_claim_success(self):
        class Bad(FakeSdk):
            def run(self, prompt, session_id):
                return Mock(final_response="probably", finish_reason="error")
        client = LocalHarness("http://127.0.0.1:8080", "qwen", factory=Bad)
        with self.assertRaises(HarnessUnavailable):
            client.run("hi", "a" * 32)
        self.assertTrue(Bad.created[0].closed)


class AlfredSdkIntegrationTests(unittest.TestCase):
    def make_app(self, home):
        cfg = load_config()
        cfg["runtime"]["database"] = str(Path(home)/"alfred.db")
        cfg["runtime"]["logs"] = home
        cfg["runtime"]["harness_enabled"] = True
        app = Alfred(cfg, load_models=False)
        app.feedback.tell = Mock()
        app.cactus = Mock()
        return app

    def test_needle_unsupported_uses_single_readonly_sdk_turn_and_session(self):
        with tempfile.TemporaryDirectory() as home:
            app = self.make_app(home)
            fake = Mock()
            fake.run.return_value = {"message":"hello", "results":[]}
            app.harness = fake
            app.supervisor.ensure_running = Mock()
            app.system2.reason = Mock()
            app.cactus.route.return_value = {"confidence":.9, "function_calls":[]}
            try:
                app.handle_text("What is an atom?")
                app.system2.reason.assert_not_called()
                app.supervisor.ensure_running.assert_called_once()
                self.assertEqual(fake.run.call_args.args[1], app.session_id)
                self.assertEqual(len(app.sessions.list_tasks(app.session_id)), 1)
                events = app.sessions.events(app.sessions.list_tasks(app.session_id)[0]["id"])
                self.assertIn("escalation", [x.kind for x in events])
            finally:
                app.close()
            fake.close.assert_called_once()

    def test_actionable_needles_delegation_proposes_into_existing_system2(self):
        with tempfile.TemporaryDirectory() as home:
            app = self.make_app(home)
            fake = Mock()
            fake.plan.return_value = {
                "message": "Ready for approval", "steps": [
                    {"tool": "get_gpu_status", "args": {}}], "save_as": None}
            app.harness = fake
            app.supervisor.ensure_running = Mock()
            app.cactus.route.return_value = {
                "confidence": .98,
                "function_calls": [{"name": "delegate_to_reasoner", "arguments": {}}]}
            app.system2.execute_or_stage = Mock(return_value={
                "message": "Completed", "results":[{"available": False}]})
            try:
                app.handle_text("Alfred explain GPU status")
                fake.plan.assert_called_once()
                self.assertEqual(fake.plan.call_args.args[1], app.session_id)
                app.system2.execute_or_stage.assert_called_once_with(
                    [{"tool":"get_gpu_status","args":{}}],
                    "Ready for approval", None, allow_actions=True)
            finally:
                app.close()

    def test_harness_tool_spoof_cannot_skip_system2_preflight(self):
        with tempfile.TemporaryDirectory() as home:
            app = self.make_app(home)
            fake = Mock()
            fake.plan.return_value = {
                "message": "shell", "steps": [
                    {"tool":"arbitrary_shell","args":{"command":"id"}}],
                "save_as": None}
            app.harness = fake
            app.supervisor.ensure_running = Mock()
            app.cactus.route.return_value = {
                "confidence": .99,
                "function_calls": [{"name": "delegate_to_reasoner", "arguments": {}}]}
            app.system2.execute_or_stage = Mock()
            app.system2.reason = Mock(return_value={"message":"Denied","results":[]})
            try:
                app.handle_text("Run shell")
                app.system2.execute_or_stage.assert_not_called()
                app.system2.reason.assert_called_once_with(
                    "Run shell", allow_actions=True)
            finally:
                app.close()

    def test_actionable_needle_call_never_goes_through_harness(self):
        with tempfile.TemporaryDirectory() as home:
            app = self.make_app(home)
            fake = Mock()
            app.harness = fake
            app.cactus.route.return_value = {
                "confidence": .99,
                "function_calls": [{"name": "run_named_task",
                                    "arguments": {"name": "danger"}}]}
            app.system2.direct = Mock(return_value={
                "message": "Local approval required", "approval": {
                    "approval_id": 1, "actions": "run_named_task"}})
            try:
                app.handle_text("run dangerous task")
                fake.run.assert_not_called()
                app.system2.direct.assert_called_once()
            finally:
                app.close()

    def test_sdk_crash_falls_back_without_new_tool_grants(self):
        with tempfile.TemporaryDirectory() as home:
            app = self.make_app(home)
            fake = Mock()
            fake.run.side_effect = HarnessUnavailable("SDK crash")
            app.harness = fake
            app.supervisor.ensure_running = Mock()
            app.cactus.route.return_value = {"confidence": .95, "function_calls": []}
            app.system2.reason = Mock(return_value={"message":"fallback", "results":[]})
            try:
                app.handle_text("simple explanation")
                fake.run.assert_called_once()
                app.system2.reason.assert_called_once_with(
                    "simple explanation", allow_actions=False)
            finally:
                app.close()

    def test_disabled_flag_preserves_old_path(self):
        cfg = load_config()
        self.assertFalse(cfg["runtime"]["harness_enabled"])
        with tempfile.TemporaryDirectory() as home:
            cfg["runtime"]["database"] = str(Path(home)/"alfred.db")
            cfg["runtime"]["logs"] = home
            app = Alfred(cfg, load_models=False)
            try:
                self.assertIsNone(app.harness)
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
