"""Harness default and fail-closed regression checks (no native models)."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from alfred.app import Alfred
from alfred.config import load_config
from alfred.cognitive.harness_sdk import HarnessUnavailable, LocalHarness


class HarnessRequiredTests(unittest.TestCase):
    def test_defaults_are_required_and_cognitive_features_enabled(self):
        runtime=load_config()["runtime"]
        self.assertNotIn("harness_enabled",runtime)
        for flag in ("needle_loop_enabled","escalation_enabled",
                     "context_enabled","resource_admission_enabled"):
            self.assertIs(runtime[flag],True)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"config.json"
            path.write_text(json.dumps({"runtime":{"harness_enabled":False}}))
            with self.assertRaises(ValueError):
                load_config(path)

    def test_missing_sdk_is_not_replanned_through_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg=load_config()
            cfg["runtime"]["database"]=str(Path(tmp)/"data.db")
            cfg["runtime"]["logs"]=tmp
            cfg["runtime"]["resource_admission_enabled"]=False
            app=Alfred(cfg,load_models=False)
            app.supervisor.ensure_running=Mock()
            app.system2.reason=Mock(side_effect=AssertionError("legacy call"))
            app.system2.execute_or_stage=Mock()
            app.harness.plan=Mock(side_effect=HarnessUnavailable("SDK missing"))
            try:
                app._start_task("reason",12)
                with self.assertRaises(HarnessUnavailable):
                    app.reason("explain heat")
                app.system2.reason.assert_not_called()
                app.system2.execute_or_stage.assert_not_called()
            finally:
                app.close()

    def test_model_id_switch_before_sdk_start_only(self):
        class FakeSdk:
            def __init__(self, **kwargs):
                self.kwargs=kwargs
            def run(self, prompt, session_id):
                return Mock(final_response="ok", finish_reason="completed")
            def close(self):
                pass
        client=LocalHarness("http://127.0.0.1:8080","Qwen",
                            factory=FakeSdk)
        try:
            client.configure_model("Qwen3.5-4B-Q4_K_M.gguf")
            self.assertEqual(client.run("hello","a"*32)["message"],"ok")
            with self.assertRaisesRegex(HarnessUnavailable,"after SDK startup"):
                client.configure_model("other-model")
        finally:
            client.close()

    def test_structural_mock_cannot_be_claimed_as_production_sdk(self):
        client=LocalHarness("http://127.0.0.1:8080","Qwen3.5-4B",
                            factory=lambda **kw:None)
        try:
            with self.assertRaisesRegex(HarnessUnavailable,"Mock SDK"):
                client.require_ready()
        finally:
            client.close()

    def test_missing_package_rejected(self):
        from importlib.metadata import PackageNotFoundError
        client=LocalHarness("http://127.0.0.1:8080","Qwen3.5-4B")
        try:
            with patch("alfred.cognitive.harness_sdk.importlib.metadata.version",
                       side_effect=PackageNotFoundError("sdk")):
                with self.assertRaisesRegex(HarnessUnavailable,"pip install"):
                    client.audit_installation()
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
