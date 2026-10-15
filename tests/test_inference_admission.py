"""Regression tests for opt-in single-slot local inference admission."""
from __future__ import annotations
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch

from alfred.inference_admission import InferenceAdmission, AdmissionRejected, linux_resources
from alfred.app import Alfred
from alfred.config import load_config
from alfred.models import Qwen
from alfred.cognitive.harness_sdk import LocalHarness

SAFE={"available_mib":1600.0,"swap_used_mib":0.,"cpu_load_per_core":.2}
LOW={"available_mib":150.0,"swap_used_mib":0.,"cpu_load_per_core":.2}


def gate(*,sample=SAFE,wait=80):
    return InferenceAdmission(
        enabled=True,sampler=lambda:dict(sample),max_wait_ms=wait)


class AdmissionUnitTests(unittest.TestCase):
    def test_default_disabled_and_no_probe_or_lock(self):
        sampler=Mock(side_effect=AssertionError("must not sample"))
        adm=InferenceAdmission(sampler=sampler)
        with adm.claim("needle"):
            with adm.claim("qwen"):
                self.assertFalse(adm.snapshot()["enabled"])
        sampler.assert_not_called()

    def test_strict_invalid_thresholds_and_modes(self):
        for kw in ({"enabled":1},{"min_available_mib":True},
                   {"min_available_mib":12},{"max_swap_used_mib":-1},
                   {"max_cpu_load_per_core":float("inf")},
                   {"max_wait_ms":False}):
            with self.subTest(kw=kw),self.assertRaises(ValueError):
                InferenceAdmission(**kw)
        with self.assertRaises(ValueError):
            with gate().claim("shell"):
                pass

    def test_low_resource_swap_cpu_and_unknown_fail_closed(self):
        samples=(LOW,
                 {"available_mib":1000,"swap_used_mib":2000,"cpu_load_per_core":.2},
                 {"available_mib":1000,"swap_used_mib":0,"cpu_load_per_core":5},
                 {"available_mib":None,"swap_used_mib":0,"cpu_load_per_core":.2})
        for sample in samples:
            a=gate(sample=sample)
            with self.subTest(sample=sample),self.assertRaises(AdmissionRejected):
                with a.claim("qwen"):
                    pass
            self.assertEqual(a.snapshot()["granted"],0)

    def test_priority_whistle_beats_waiting_qwen(self):
        a=gate(wait=700)
        entered=threading.Event()
        release=threading.Event()
        admitted=[]
        def needle():
            with a.claim("needle"):
                entered.set()
                release.wait(2)
        worker=threading.Thread(target=needle)
        worker.start()
        self.assertTrue(entered.wait(1))
        def qwen():
            with a.claim("qwen"):
                admitted.append("qwen")
        q=threading.Thread(target=qwen)
        q.start()
        time.sleep(.015)
        def whistle():
            with a.claim("whistle"):
                admitted.append("whistle")
        w=threading.Thread(target=whistle)
        w.start()
        release.set()
        for t in (worker,q,w): t.join(2)
        self.assertFalse(any(t.is_alive() for t in (worker,q,w)))
        self.assertEqual(admitted,["whistle","qwen"])
        self.assertFalse(a.snapshot()["busy"])

    def test_voice_capture_defers_qwen_and_never_blocks_scheduler(self):
        a=gate(wait=20)
        a.set_capture_active(True)
        with self.assertRaises(AdmissionRejected):
            with a.claim("qwen"):
                self.fail("Qwen admitted during capture")
        self.assertTrue(a.snapshot()["capture_active"])
        with a.claim("whistle"):
            self.assertTrue(a.snapshot()["busy"])
        a.set_capture_active(False)
        with a.claim("qwen"):
            pass
        self.assertEqual(a.snapshot()["granted"],2)

    def test_optional_diagnostics_shed_under_capture_or_pressure(self):
        a=gate()
        self.assertTrue(a.allow_optional_diagnostics())
        a.set_capture_active(True)
        self.assertFalse(a.allow_optional_diagnostics())
        a.set_capture_active(False)
        with a.claim("needle"):
            self.assertFalse(a.allow_optional_diagnostics())
        b=gate(sample=LOW)
        self.assertFalse(b.allow_optional_diagnostics())

    def test_busy_queue_rejects_without_retry_or_side_effects(self):
        a=gate(wait=20)
        started=threading.Event()
        release=threading.Event()
        executions=[]
        def held():
            with a.claim("qwen"):
                started.set()
                release.wait(2)
        t=threading.Thread(target=held);t.start()
        self.assertTrue(started.wait(1))
        with self.assertRaises(AdmissionRejected):
            with a.claim("needle"):
                executions.append("never executed")
        self.assertEqual(executions,[])
        release.set()
        t.join(2)
        self.assertEqual(a.snapshot()["queue_depth"],0)

    def test_shutdown_unblocks_waiter_without_retry(self):
        a=gate(wait=800)
        holding=threading.Event()
        release=threading.Event()
        reported=[]
        def first():
            with a.claim("needle"):
                holding.set()
                release.wait(2)
        def second():
            try:
                with a.claim("qwen"):
                    reported.append("executed")
            except AdmissionRejected:
                reported.append("cancelled")
        t=threading.Thread(target=first);t.start()
        self.assertTrue(holding.wait(1))
        waiter=threading.Thread(target=second);waiter.start()
        time.sleep(.025)
        a.close()
        release.set()
        waiter.join(2);t.join(2)
        self.assertEqual(reported,["cancelled"])
        self.assertFalse(a.snapshot()["busy"])
        with self.assertRaises(AdmissionRejected):
            with a.claim("qwen"):
                pass

    def test_bounded_private_snapshot_has_no_user_content(self):
        a=gate()
        with a.claim("needle"):
            pass
        s=a.snapshot()
        self.assertEqual(s["granted"],1)
        self.assertLess(len(json.dumps(s)),600)
        for key in ("transcript","prompt","token","tool_args","model_response"):
            self.assertNotIn(key,s)

    def test_os_resource_reader_bounded(self):
        s=linux_resources()
        self.assertEqual(set(s),{"available_mib","swap_used_mib","cpu_load_per_core"})
        for value in s.values():
            self.assertTrue(value is None or value>=0)


class IntegrationAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        cfg=load_config()
        cfg["runtime"]["database"]=str(Path(self.tmp.name)/"local.db")
        cfg["runtime"]["logs"]=self.tmp.name
        cfg["runtime"]["resource_admission_enabled"]=True
        self.app=Alfred(cfg,load_models=False)
        self.app.feedback.tell=Mock()
        self.app.cactus=Mock()
        self.app.admission.sampler=lambda:dict(SAFE)

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def test_model_resource_pressure_rejects_before_tool_execution(self):
        # This proves a real model with the injected shared gate cannot run
        # while under pressure; app mocks alone cannot attest native safety.
        q=Qwen(self.app.cfg["qwen"],Mock(),admission=self.app.admission)
        self.app.admission.sampler=lambda:dict(LOW)
        with patch("alfred.models.request_json") as request:
            with self.assertRaises(AdmissionRejected):
                q.complete("Explain current status",[])
            request.assert_not_called()
        self.assertEqual(self.app.system2.approvals.pending(),[])

    def test_no_second_managed_backend_spawn_under_admission(self):
        with patch.object(self.app.supervisor,"ensure_running",return_value=True) as ensure:
            with patch.object(self.app.supervisor,"model_id",return_value="fake-model"):
                q=Qwen(self.app.cfg["qwen"],self.app.supervisor,admission=self.app.admission)
                with patch("alfred.models.request_json",return_value={
                    "choices":[{"message":{"content":json.dumps({
                        "message":"Okay","steps":[],"save_as":None})}}]}):
                    q.complete("What is measured?",[])
            ensure.assert_called_once()

    def test_off_feature_keeps_current_needledirect_behavior(self):
        self.app.cactus.route.return_value={
            "confidence":.97,"function_calls":[{"name":"get_gpu_status","arguments":{}}]}
        self.app.system2.direct=Mock(return_value={
            "message":"done","results":[{"available":True,"gpu_temp_c":55}]})
        self.app.handle_text("GPU status")
        self.app.system2.direct.assert_called_once()
        self.assertEqual(len(self.app.sessions.list_tasks(self.app.session_id)),1)


if __name__=="__main__":
    unittest.main()
