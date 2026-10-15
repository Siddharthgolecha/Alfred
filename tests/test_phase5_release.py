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
    def test_12_families_144_repeat_or_perturbation_trial_ids(self):
        a=list(cases())
        self.assertEqual(len(a),144)
        self.assertEqual(a,list(cases()))
        self.assertEqual(set(x["scenario"] for x in a),set(CATEGORIES))
        self.assertEqual(len(set(x["id"] for x in a)),144)
        self.assertEqual(len(MODES),3)
        # IDs are reproducible trial labels, not distinct semantic tasks.
        self.assertEqual(len({x["text"] for x in a}),21)

    def test_offline_stub_never_authorizes_rollout_even_if_metrics_fabricated(self):
        metrics=("p50_latency_ms","p95_latency_ms","qwen_cold_load_ms",
                 "qwen_warm_load_ms","peak_ram_mib","peak_swap_used_mib",
                 "peak_cpu_load_per_core","peak_gpu_utilization_pct")
        fake={"native_jetson_tested":False,"device":"NVIDIA Jetson Orin Nano 8GB",
              "fixture_count":144,"distinct_semantic_task_count":None,
              "sources":["CI mocked policy run"],
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
             "fixture_count":144,"distinct_semantic_task_count":120,
             "sources":["locally run native suite"],
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

    def test_distinct_semantic_tasks_are_required_even_with_144_trials(self):
        """Synthetic all-green declarations cannot launder repeated IDs as tasks."""
        metrics=("p50_latency_ms","p95_latency_ms","qwen_cold_load_ms",
                 "qwen_warm_load_ms","peak_ram_mib","peak_swap_used_mib",
                 "peak_cpu_load_per_core","peak_gpu_utilization_pct")
        report={
            "native_jetson_tested":True,
            "device":"NVIDIA Jetson Orin Nano 8GB",
            "fixture_count":144,
            "distinct_semantic_task_count":120,
            "sources":["synthetic-only declaration, not hardware attestation"],
            "models_real":True,"offline":True,
            "voice_capture_deadline_passed":True,
            "scheduler_deadline_passed":True,
            "clean_owned_shutdown":True,
            "dependencies_merged":True,
            "release_gates":{
                "unauthorized_tool_executions":0,
                "unintended_replay":0,
                "plugin_escape_attempts_succeeded":0},
            **{m:10.0 for m in metrics}
        }
        thresholds={"max_"+m:11.0 for m in metrics}
        # Purely a schema/policy positive control: the values above are
        # fabricated, and do not establish a real hardware-release pass.
        self.assertTrue(evaluate_release(
            report,thresholds,acknowledge_hardware=True)["release_allowed"])
        for distinct in (None,False,True,0,-1,12,99,99.0,
                         120.0,"120",[],{},float("nan")):
            with self.subTest(distinct=distinct):
                bad=dict(report,distinct_semantic_task_count=distinct)
                verdict=evaluate_release(
                    bad,thresholds,acknowledge_hardware=True)
                self.assertFalse(verdict["release_allowed"])
                self.assertIn("Insufficient distinct semantic task coverage",
                              verdict["reasons"])
                with self.assertRaises(ReleaseBlocked):
                    require_release(bad,thresholds,acknowledge_hardware=True)
        missing=dict(report)
        missing.pop("distinct_semantic_task_count")
        self.assertIn("Insufficient distinct semantic task coverage",
                      evaluate_release(missing,thresholds,
                                       acknowledge_hardware=True)["reasons"])
        for distinct in (145,10000):
            with self.subTest(impossible_count=distinct):
                bad=dict(report,distinct_semantic_task_count=distinct)
                verdict=evaluate_release(
                    bad,thresholds,acknowledge_hardware=True)
                self.assertFalse(verdict["release_allowed"])
                self.assertIn("Distinct semantic task count exceeds recorded cases",
                              verdict["reasons"])
        repeated=dict(
            report,distinct_semantic_task_count=12,
            distinct_semantic_families=12,
            trial_ids_are_distinct_tasks=False,
            physical_distinct_task_coverage=None)
        self.assertFalse(evaluate_release(
            repeated,thresholds,acknowledge_hardware=True)["release_allowed"])
        for count in (None,False,True,99,"144",0):
            with self.subTest(fixture_count=count):
                bad=dict(report,fixture_count=count)
                self.assertFalse(evaluate_release(
                    bad,thresholds,acknowledge_hardware=True)["release_allowed"])
        bad=dict(report,fixture_count=100,distinct_semantic_task_count=101)
        self.assertFalse(evaluate_release(
            bad,thresholds,acknowledge_hardware=True)["release_allowed"])
        min_valid=dict(report,fixture_count=100,
                       distinct_semantic_task_count=100)
        self.assertTrue(evaluate_release(
            min_valid,thresholds,acknowledge_hardware=True)["release_allowed"])

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
            self.assertEqual(report["distinct_semantic_families"],12)
            self.assertIsNone(report["distinct_semantic_task_count"])
            self.assertFalse(report["trial_ids_are_distinct_tasks"])
            self.assertIsNone(report["physical_distinct_task_coverage"])
            self.assertFalse(report["native_jetson_tested"])
            self.assertEqual(report["release_gates"]["unauthorized_tool_executions"],0)
            self.assertEqual(report["release_gates"]["unintended_replay"],0)
            raw=list(__import__("csv").DictReader((Path(td)/"raw.csv").open()))
            self.assertEqual(len(raw),432)
            self.assertIn("offline_python_stubs",(Path(td)/"report.json").read_text())
            self.assertTrue((Path(td)/"fixtures.json").exists())
            baseline=[r for r in raw if r["mode"]==MODES[0]]
            unified=[r for r in raw if r["mode"]==MODES[2]]
            self.assertEqual(len(unified),144)
            self.assertTrue(all(r["gate_enabled"]=="0" for r in baseline))
            self.assertTrue(all(r["gate_enabled"]=="1" for r in unified))
            self.assertGreater(sum(int(r["gate_granted"]) for r in unified),0)
            self.assertGreater(sum(int(r["gate_whistle_calls"]) for r in unified),0)
            for case in ("ram","swap","cpu"):
                overloaded=[r for r in unified if r["gate_resource_case"]==case]
                self.assertEqual(len(overloaded),12)
                # Unknown/stale scoped pronouns are rejected BEFORE inference,
                # so the admission sampler need not be called in that case.
                # The other 11 families still exercise the actual pressure gate.
                exercised=[r for r in overloaded
                           if r["scenario"]!="context_pronoun"]
                self.assertEqual(len(exercised),11)
                self.assertTrue(all(int(r["gate_pressure"])>=1 for r in exercised))
                early=[r for r in overloaded if r["scenario"]=="context_pronoun"]
                self.assertEqual(len(early),1)
                self.assertEqual(early[0]["task_state"],"rejected")
                self.assertEqual(early[0]["accepted_tools"],"0")
            contested=[r for r in unified if r["gate_capture_contended"]=="1"]
            self.assertEqual(len(contested),12)
            # A context pronoun without any verified prior read fails
            # in the trusted resolver, before the model/gate is consulted.
            admitted=[r for r in contested if r["scenario"]!="context_pronoun"]
            self.assertEqual(len(admitted),11)
            self.assertTrue(all(int(r["gate_rejected"])>=1 for r in admitted))
            denied=[r for r in contested if r["scenario"]=="context_pronoun"]
            self.assertEqual(len(denied),1)
            self.assertEqual(denied[0]["task_state"],"rejected")
            self.assertEqual(denied[0]["accepted_tools"],"0")
            self.assertGreater(sum(int(r["gate_needle_attempts"]) for r in unified),0)
            for mode in MODES[1:]:
                enhanced=[r for r in raw if r["mode"]==mode]
                self.assertTrue(all(x["context_enabled"]=="1" for x in enhanced))
                self.assertGreater(sum(int(x["context_seeded_verified"])
                                       for x in enhanced),0)
                self.assertGreater(sum(int(x["context_packet_verified_referent"])
                                       for x in enhanced),0)
                self.assertEqual(sum(int(x["context_ttl_expiry_blocked"])
                                     for x in enhanced),1)
                self.assertGreater(sum(int(x["context_cross_owner_blocked"])
                                       for x in enhanced),0)
                self.assertGreater(sum(int(x["context_meeting_mode_blocked"])
                                       for x in enhanced),0)
            self.assertTrue(all(x["context_enabled"]=="0" for x in baseline))
            summary=report["measurements"][MODES[2]]
            self.assertEqual(summary["admission_pressure_refusals"],
                             sum(int(r["gate_pressure"]) for r in unified))


if __name__=="__main__":
    unittest.main()
