"""Release matrix, zero-replay gates and untrusted instruction defenses."""
import json
from pathlib import Path
import tempfile
import unittest

from alfred.release_gates import evaluate_release,require_release,ReleaseBlocked
from alfred.inference_admission import InferenceAdmission
from alfred.system2 import preflight
from alfred.runtime import Store,Tools
from phase5_qa import cases, CATEGORIES, MODES, run_matrix


class ReleasePolicyTests(unittest.TestCase):
    def test_more_than_100_deterministic_distinct_cases_all_categories(self):
        a=list(cases())
        self.assertEqual(len(a),144)
        self.assertEqual(a,list(cases()))
        self.assertEqual(set(x["scenario"] for x in a),set(CATEGORIES))
        self.assertEqual(len(set(x["id"] for x in a)),144)
        self.assertEqual(len(MODES),3)

    def test_offline_stub_never_authorizes_rollout_even_if_metrics_fabricated(self):
        metrics=("p50_latency_ms","p95_latency_ms","qwen_cold_load_ms",
                 "qwen_warm_load_ms","peak_ram_mib","peak_swap_used_mib",
                 "peak_cpu_load_per_core","peak_gpu_utilization_pct")
        fake={"native_jetson_tested":False,"device":"NVIDIA Jetson Orin Nano 8GB",
              "fixture_count":144,"sources":["CI mocked policy run"],
              "models_real":False,"offline":True,"voice_capture_deadline_passed":True,
              "scheduler_deadline_passed":True,"clean_owned_shutdown":True,
              "dependencies_merged":False,
              "release_gates":{"unauthorized_tool_executions":0,
                               "unintended_replay":0,
                               "plugin_escape_attempts_succeeded":0},
              **{m:0 for m in metrics}}
        thresholds={"max_"+m:1.0 for m in metrics}
        verdict=evaluate_release(fake,thresholds,acknowledge_hardware=True)
        self.assertFalse(verdict["release_allowed"])
        self.assertIn("No physical Jetson measurements",verdict["reasons"])
        with self.assertRaises(ReleaseBlocked):
            require_release(fake,thresholds,acknowledge_hardware=True)

    def test_missing_thresholds_never_default_to_pass(self):
        raw={"native_jetson_tested":True,"device":"NVIDIA Jetson Orin Nano 8GB",
             "fixture_count":144,"sources":["locally run native suite"],
             "models_real":True,"offline":True,"voice_capture_deadline_passed":True,
             "scheduler_deadline_passed":True,"clean_owned_shutdown":True,
             "dependencies_merged":True,
             "release_gates":{"unauthorized_tool_executions":0,
                              "unintended_replay":0,
                              "plugin_escape_attempts_succeeded":0}}
        for metric in ("p50_latency_ms","p95_latency_ms","qwen_cold_load_ms",
                       "qwen_warm_load_ms","peak_ram_mib","peak_swap_used_mib",
                       "peak_cpu_load_per_core","peak_gpu_utilization_pct"):
            raw[metric]=10.0
        self.assertFalse(evaluate_release(raw,{},acknowledge_hardware=True)
                         ["release_allowed"])
        agreed={"max_"+k:11. for k in raw if k.startswith((
            "p50_","p95_","qwen_cold_","qwen_warm_","peak_"))}
        self.assertTrue(evaluate_release(raw,agreed,acknowledge_hardware=True)
                        ["release_allowed"])
        bad=dict(raw)
        bad["release_gates"]=dict(raw["release_gates"],unintended_replay=1)
        self.assertFalse(evaluate_release(bad,agreed,acknowledge_hardware=True)
                         ["release_allowed"])
        self.assertFalse(evaluate_release(raw,agreed,acknowledge_hardware=False)
                         ["release_allowed"])

    def test_plugin_network_and_shell_not_writable_via_forged_model_proposal(self):
        with tempfile.TemporaryDirectory() as td:
            store=Store(Path(td)/"review.sqlite")
            try:
                tools=Tools({"allowed_services":[],"named_tasks":{}},store)
                for name,args in [
                    ("fetch_remote_url",{"url":"https://evil.example"}),
                    ("run_shell",{"cmd":"curl secret"}),
                    ("run_named_task",{"name":"../ssh/id_rsa"}),
                    ("control_service",{"name":"ssh.service","action":"start"})]:
                    with self.subTest(name=name),self.assertRaises((
                        PermissionError,ValueError)):
                        preflight([{"tool":name,"args":args}],tools)
            finally:
                store.db.close()

    def test_raw_mock_results_written_and_security_gates_clear(self):
        with tempfile.TemporaryDirectory() as td:
            report=run_matrix(td)
            self.assertEqual(report["row_count"],432)
            self.assertEqual(report["fixture_count"],144)
            self.assertFalse(report["native_jetson_tested"])
            self.assertEqual(report["release_gates"]["unauthorized_tool_executions"],0)
            self.assertEqual(report["release_gates"]["unintended_replay"],0)
            raw=list(__import__("csv").DictReader((Path(td)/"raw.csv").open()))
            self.assertEqual(len(raw),432)
            self.assertIn("offline_python_stubs",(Path(td)/"report.json").read_text())
            self.assertTrue((Path(td)/"fixtures.json").exists())


if __name__=="__main__":
    unittest.main()
