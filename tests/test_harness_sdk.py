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
    LocalHarness, HarnessUnavailable, check_safe_patch, DENIED_ROWS,
    EXPECTED_ACTIVE_ROWS, EXPECTED_DISABLED_ROWS, audit_effective_cordis_config,
    SDK_VERSION)


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

    def test_exact_effective_cordis_audit_rejects_bypass_and_spoof(self):
        def fixture():
            rows = []
            for key, plugin in EXPECTED_ACTIVE_ROWS.items():
                rows.extend(("- id: " + key, "  name: '" + plugin + "'"))
            for key, plugin in EXPECTED_DISABLED_ROWS.items():
                rows.extend(("- id: " + key, "  name: '" + plugin + "'",
                             "  disabled: true"))
            return "\\n".join(rows) + "\\n"
        good = fixture()
        expected = audit_effective_cordis_config(good)
        self.assertIn("persistent-bash", expected["disabled"])
        candidates = [
            good.replace("  disabled: true", "  disabled: false", 1),
            good.replace("  disabled: true", "  disabled: !!js false", 1),
            good.replace("  name: '@deepseek-ai/dsh-tools'",
                         "  name: '@evil/executor'", 1),
            good + "- id: rogue-tool\\n  name: '@rogue/shell'\\n",
            good + "- id: tools\\n  name: '@deepseek-ai/dsh-tools'\\n",
            good.replace("- id: persistent-bash\\n", "", 1),
            good.replace("  name: '@deepseek-ai/dsh-tool-bash-persistent'",
                         "  name: '@evil/danger'", 1),
        ]
        for bad in candidates:
            with self.subTest(case=bad[-90:]), self.assertRaises(HarnessUnavailable):
                audit_effective_cordis_config(bad)

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

    def test_runtime_exception_does_not_echo_secret_or_retain_temp_home(self):
        secret = "private-test-token-ABC123"
        class Exploding(FakeSdk):
            def run(self, prompt, session_id):
                raise RuntimeError(secret + " " + prompt)
        client = LocalHarness("http://127.0.0.1:8080", "qwen", factory=Exploding)
        with self.assertRaises(HarnessUnavailable) as exc:
            client.run("do not log " + secret, "a" * 32)
        self.assertNotIn(secret, str(exc.exception))
        self.assertIn("unavailable", str(exc.exception))
        sdk = Exploding.created[0]
        self.assertTrue(sdk.closed)
        self.assertFalse(Path(sdk.kwargs["dsh_home"]).parent.exists())

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
                app.system2.reason.assert_not_called()
                # An invalid model plan fails closed, instead of granting a
                # fresh chance to reinterpret a forged tool request.
                self.assertEqual(
                    app.sessions.list_tasks(app.session_id)[0]["state"], "failed")
            finally:
                app.close()

    def test_execution_exception_cannot_replay_after_partial_side_effect(self):
        with tempfile.TemporaryDirectory() as home:
            app = self.make_app(home)
            fake = Mock()
            fake.plan.return_value = {
                "message": "Approved read", "steps": [
                    {"tool": "get_gpu_status", "args": {}}], "save_as": None}
            app.harness = fake
            app.supervisor.ensure_running = Mock()
            app.cactus.route.return_value = {
                "confidence": .98,
                "function_calls": [{"name": "delegate_to_reasoner", "arguments": {}}]}
            side_effects = []
            def partial_execute(*args, **kwargs):
                side_effects.append("already-happened")
                raise RuntimeError("tool failed after external effect")
            app.system2.execute_or_stage = Mock(side_effect=partial_execute)
            app.system2.reason = Mock(side_effect=AssertionError("replayed side effect"))
            try:
                app.handle_text("Read GPU status and analyze")
                self.assertEqual(side_effects, ["already-happened"])
                app.system2.execute_or_stage.assert_called_once()
                app.system2.reason.assert_not_called()
                fake.plan.assert_called_once()
                self.assertEqual(
                    app.sessions.list_tasks(app.session_id)[0]["state"], "failed")
            finally:
                app.close()

    def test_sensitive_plan_staged_once_and_not_auto_approved(self):
        with tempfile.TemporaryDirectory() as home:
            app = self.make_app(home)
            app.tools.tasks["safe-task"] = ["/bin/echo", "ok"]
            fake = Mock()
            fake.plan.return_value = {
                "message": "Request approval",
                "steps": [{"tool": "run_named_task",
                           "args": {"name": "safe-task"}}], "save_as": None}
            app.harness = fake
            app.supervisor.ensure_running = Mock()
            app.cactus.route.return_value = {
                "confidence": .98,
                "function_calls": [{"name": "delegate_to_reasoner", "arguments": {}}]}
            app.tools.run_plan = Mock(side_effect=AssertionError("no auto execution"))
            app.system2.approve = Mock(side_effect=AssertionError("no auto approval"))
            try:
                app.handle_text("Run my approved named task")
                app.tools.run_plan.assert_not_called()
                app.system2.approve.assert_not_called()
                pending = app.system2.approvals.pending()
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["actions"], "run_named_task")
                self.assertEqual(app.sessions.list_tasks(app.session_id)[0]["state"],
                                 "pending_approval")
                task_id = app.sessions.list_tasks(app.session_id)[0]["id"]
                self.assertIn("approval", [e.kind for e in app.sessions.events(task_id)])
                self.assertEqual(app.store.db.execute(
                    "SELECT status FROM approvals WHERE id=?",
                    (pending[0]["id"],)).fetchone()[0], "pending")
            finally:
                app.close()

    def test_generation_crash_may_fallback_before_any_execution(self):
        with tempfile.TemporaryDirectory() as home:
            app = self.make_app(home)
            fake = Mock()
            fake.plan.side_effect = HarnessUnavailable("SDK exited before a plan")
            app.harness = fake
            app.supervisor.ensure_running = Mock()
            app.system2.execute_or_stage = Mock()
            app.system2.reason = Mock(return_value={"message":"Fallback","results":[]})
            try:
                out = app.reason("Find an answer", allow_actions=True)
                self.assertEqual(out["message"], "Fallback")
                app.system2.reason.assert_called_once_with(
                    "Find an answer", allow_actions=True)
                app.system2.execute_or_stage.assert_not_called()
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
