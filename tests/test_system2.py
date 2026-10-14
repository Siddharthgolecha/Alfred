"""Phase 2 tests: no native models, microphone, GGUF, or Jetson required."""
from pathlib import Path
import json
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from alfred.config import load_config
from alfred.models import Qwen
from alfred.runtime import Store, Tools, TOOL_SCHEMAS
from alfred.system2 import Approvals, System2, parse_plan, preflight


class System2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/"alfred.db")
        self.cfg={"allowed_services":["alfred-monitor.service"],"named_tasks":{"safe_echo":["/bin/echo","ok"]},
                  "max_plan_steps":5,"approval_ttl_seconds":60}
        self.tools=Tools(self.cfg,self.store)
        self.qwen=Mock()
        self.sys=System2(self.qwen,self.tools,self.store,self.cfg)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_read_only_direct_executes(self):
        out=self.sys.direct([{"name":"get_system_status","arguments":{}}])
        self.assertIn("memory_mib",out["results"][0])
        self.assertEqual(self.sys.approvals.pending(),[])

    def test_direct_service_requires_local_approval(self):
        out=self.sys.direct([{"name":"control_service","arguments":{
            "name":"alfred-monitor.service","action":"stop"}}])
        self.assertIn("approval",out)
        with patch.object(self.tools,"_service",return_value={"exit_code":0}) as run:
            self.assertEqual(run.call_count,0)
            result=self.sys.approve(out["approval"]["approval_id"])
            self.assertEqual(len(result),1)
            run.assert_called_once()
        with self.assertRaises(ValueError):
            self.sys.approve(out["approval"]["approval_id"])

    def test_unapproved_service_rejected_before_first_side_effect(self):
        with self.assertRaises(PermissionError):
            self.sys.direct([
                {"name":"create_reminder","arguments":{"message":"must not schedule","delay_minutes":2}},
                {"name":"control_service","arguments":{"name":"ssh.service","action":"stop"}}
            ])
        self.assertEqual(self.store.list_reminders(),[])

    def test_named_task_preflight(self):
        with self.assertRaises(PermissionError):
            self.sys.direct([{"name":"run_named_task","arguments":{"name":"not_registered"}}])
        out=self.sys.direct([{"name":"run_named_task","arguments":{"name":"safe_echo"}}])
        self.assertIn("approval",out)

    def test_expired_proposal_cannot_run(self):
        pid=self.sys.direct([{"name":"run_named_task","arguments":{"name":"safe_echo"}}])["approval"]["approval_id"]
        with self.store.lock,self.store.db:
            self.store.db.execute("UPDATE approvals SET expires_at=? WHERE id=?",(int(time.time())-1,pid))
        with self.assertRaises(ValueError):
            self.sys.approve(pid)
        self.assertEqual(self.sys.approvals.pending(),[])

    def test_rejected_proposal_cannot_run(self):
        pid=self.sys.direct([{"name":"run_named_task","arguments":{"name":"safe_echo"}}])["approval"]["approval_id"]
        self.assertTrue(self.sys.approvals.reject(pid))
        self.assertFalse(self.sys.approvals.reject(pid))
        with self.assertRaises(ValueError):
            self.sys.approve(pid)

    def test_read_only_fallback_does_not_hide_mutation(self):
        res=self.sys.execute_or_stage([{"tool":"create_reminder","args":
                     {"message":"oops","delay_minutes":1}}],allow_actions=False)
        self.assertIn("needs an explicit actionable command",res["message"])
        self.assertEqual(self.store.list_reminders(),[])
        self.assertEqual(self.sys.approvals.pending(),[])

    def test_qwen_receives_full_tool_catalog_and_runtime_allowlists(self):
        self.qwen.complete.return_value={
            "message":"I'll check local system status","steps":[{"tool":"get_system_status","args":{}}],
            "save_as":None}
        out=self.sys.reason("Check the system")
        self.assertIn("memory_mib",out["results"][0])
        args,kwargs=self.qwen.complete.call_args
        self.assertIsInstance(args[1],list)
        self.assertIsInstance(args[1][0],dict)
        self.assertIn("parameters",args[1][0])
        self.assertEqual(list(kwargs["allowed_services"]),["alfred-monitor.service"])
        self.assertEqual(list(kwargs["named_tasks"]),["safe_echo"])

    def test_qwen_off_topic_plain_explanation(self):
        self.qwen.complete.return_value={"message":"I can explain that.","steps":[],"save_as":None}
        result=self.sys.reason("What is a qubit?",allow_actions=False)
        self.assertEqual(result["message"],"I can explain that.")
        self.assertEqual(result["results"],[])

    def test_unknown_qwen_tool_cannot_execute(self):
        self.qwen.complete.return_value={"message":"Done","steps":[{"tool":"run_shell","args":{"cmd":"rm -rf /"}}]}
        with self.assertRaises(ValueError):
            self.sys.reason("delete everything")

    def test_qwen_service_plan_is_staged(self):
        self.qwen.complete.return_value={
            "message":"I propose stopping the approved service",
            "steps":[{"tool":"control_service","args":
                      {"name":"alfred-monitor.service","action":"stop"}}],"save_as":None}
        result=self.sys.reason("Stop the monitor")
        self.assertIn("approval",result)
        self.assertEqual(len(self.sys.approvals.pending()),1)

    def test_explanation_consumes_actual_tool_observations(self):
        self.qwen.complete.return_value={"message":"Checking",
             "steps":[{"tool":"get_system_status","args":{}}],"save_as":None}
        self.qwen.explain.return_value="The Jetson memory reading is in the tool result."
        result=self.sys.reason("Explain the current system memory")
        self.assertIn("memory reading",result["message"])
        self.qwen.explain.assert_called_once()
        observations=self.qwen.explain.call_args.args[1]
        self.assertIn("memory_mib",observations[0])

    def test_explanation_never_runs_when_sensitive_plan_staged(self):
        self.qwen.complete.return_value={"message":"Stopping would require approval",
             "steps":[{"tool":"control_service","args":{
                 "name":"alfred-monitor.service","action":"stop"}}],"save_as":None}
        result=self.sys.reason("Explain why and stop the monitor")
        self.assertIn("approval",result)
        self.qwen.explain.assert_not_called()

    def test_saved_workflow_is_proposed_not_immediately_committed(self):
        self.qwen.complete.return_value={"message":"Create an example workflow",
             "steps":[{"tool":"get_gpu_status","args":{}}],
             "save_as":"gpu_health"}
        result=self.sys.reason("Please create a workflow for GPU health")
        self.assertIn("approval",result)
        self.assertEqual(self.store.list_workflows(),[])
        self.sys.approve(result["approval"]["approval_id"])
        self.assertEqual(self.store.list_workflows()[0]["name"],"gpu_health")

    def test_unrequested_workflow_save_rejected(self):
        payload={"message":"oops","steps":[{"tool":"get_gpu_status","args":{}}],"save_as":"bad"}
        with self.assertRaises(ValueError):
            parse_plan(payload,"What is the GPU status?",self.tools,5)

    def test_invalid_condition_reference_rejected(self):
        with self.assertRaises(ValueError):
            preflight([{"tool":"get_gpu_status","args":{},
                "when":{"step":0,"field":"gpu_temp_c","op":"gt","value":75}}],self.tools)

    def test_run_saved_workflow_is_snapshot_staged(self):
        self.store.save_workflow("checks",[{"tool":"get_system_status","args":{}}])
        out=self.sys.direct([{"name":"run_workflow","arguments":{"name":"checks"}}])
        self.assertIn("approval",out)
        # Subsequent edits must not change the already approved snapshot.
        self.store.save_workflow("checks",[{"tool":"get_gpu_status","args":{}}])
        result=self.sys.approve(out["approval"]["approval_id"])
        self.assertIn("memory_mib",result[0])

    def test_cancel_reminder_requires_approval_and_runs_once(self):
        rid=self.store.create_reminder("verify",5)["id"]
        out=self.sys.direct([{"name":"cancel_reminder","arguments":{"reminder_id":rid}}])
        self.assertIn("approval",out)
        self.assertEqual(len(self.store.list_reminders()),1)
        result=self.sys.approve(out["approval"]["approval_id"])
        self.assertEqual(result,[{"cancelled":True}])
        self.assertEqual(self.store.list_reminders(),[])

    def test_qwen_cancel_schedule_staged(self):
        sid=self.store.create_schedule("get_system_status",10)["id"]
        self.qwen.complete.return_value={"message":"Proposed cancellation",
           "steps":[{"tool":"cancel_schedule","args":{"schedule_id":sid}}],"save_as":None}
        out=self.sys.reason("Cancel schedule number "+str(sid))
        self.assertIn("approval",out)
        self.assertEqual(len(self.store.list_schedules()),1)
        self.sys.approve(out["approval"]["approval_id"])
        self.assertEqual(self.store.list_schedules(),[])

    def test_workflow_save_rejects_cancellation(self):
        rid=self.store.create_reminder("must survive",5)["id"]
        self.qwen.complete.return_value={"message":"Save this",
          "steps":[{"tool":"cancel_reminder","args":{"reminder_id":rid}}],
          "save_as":"bad_workflow"}
        with self.assertRaises(ValueError):
            self.sys.reason("Create a workflow that cancels reminder "+str(rid))
        self.assertEqual(len(self.store.list_reminders()),1)

    def test_atomic_claim_one_time(self):
        pid=self.sys.direct([{"name":"run_named_task","arguments":{"name":"safe_echo"}}])["approval"]["approval_id"]
        self.assertEqual(self.sys.approvals.claim(pid)["id"],pid)
        with self.assertRaises(ValueError):
            self.sys.approvals.claim(pid)
        self.sys.approvals.finish(pid,error=RuntimeError("simulated failure"))
        with self.assertRaises(ValueError):
            self.sys.approve(pid)


class QwenSchemaTests(unittest.TestCase):
    def test_schema_and_tool_info_sent_to_llama(self):
        cfg=load_config()["qwen"]
        cfg["url"]="http://127.0.0.1:8080"
        supervisor=Mock()
        supervisor.model_id.return_value="qwen3.5-4b-q4_k_m.gguf"
        qwen=Qwen(cfg,supervisor)
        response={"choices":[{"message":{"content":json.dumps(
            {"message":"checking","steps":[{"tool":"get_system_status","args":{}}],
             "save_as":None})}}]}
        with patch("alfred.models.request_json",return_value=response) as send:
            out=qwen.complete("Check status",TOOL_SCHEMAS[:2],
                              allowed_services=("monitor.service",),named_tasks=("safe_task",))
        self.assertEqual(out["steps"][0]["tool"],"get_system_status")
        supervisor.ensure_running.assert_called_once()
        endpoint,payload=send.call_args.args[:2]
        self.assertEqual(endpoint,"http://127.0.0.1:8080/v1/chat/completions")
        self.assertEqual(payload["response_format"]["type"],"json_schema")
        self.assertEqual(payload["response_format"]["schema"]["type"],"object")
        self.assertIn('"name":"get_system_status"',payload["messages"][0]["content"])
        self.assertIn("monitor.service",payload["messages"][0]["content"])

    def test_explain_uses_readonly_schema_and_cites_observations(self):
        cfg=load_config()["qwen"]
        supervisor=Mock()
        supervisor.model_id.return_value=""
        qwen=Qwen(cfg,supervisor)
        fake={"choices":[{"message":{"content":json.dumps({"answer":"Observed memory is low."})}}]}
        with patch("alfred.models.request_json",return_value=fake) as send:
            result=qwen.explain("Why is memory low?",[{"memory_mib":{"MemAvailable":128}}])
        self.assertIn("Observed",result)
        payload=send.call_args.args[1]
        self.assertEqual(payload["response_format"]["schema"]["required"],["answer"])
        self.assertIn("MemAvailable",payload["messages"][1]["content"])

    def test_malformed_model_reply_rejected(self):
        cfg=load_config()["qwen"]
        supervisor=Mock()
        supervisor.model_id.return_value=""
        qwen=Qwen(cfg,supervisor)
        with patch("alfred.models.request_json",return_value={"choices":[{"message":{"content":"hello"}}]}):
            with self.assertRaises(ValueError):
                qwen.complete("command",TOOL_SCHEMAS[:2])



class RoutingTests(unittest.TestCase):
    def setUp(self):
        from alfred.app import Alfred
        self.tmp=tempfile.TemporaryDirectory()
        cfg=load_config()
        cfg["runtime"]["database"]=str(Path(self.tmp.name)/"state.db")
        cfg["runtime"]["logs"]=self.tmp.name
        self.app=Alfred(cfg,load_models=False)
        self.app.cactus=Mock()
        self.app.feedback.tell=Mock()

    def tearDown(self):
        self.app.close()
        self.app.store.db.close()
        self.tmp.cleanup()

    def test_needle_low_confidence_never_executes(self):
        self.app.cactus.route.return_value={"function_calls":[
           {"name":"create_reminder","arguments":{"message":"oops","delay_minutes":1}}
        ],"confidence":0.21}
        self.app.handle_text("Remind me tomorrow")
        self.assertEqual(self.app.store.list_reminders(),[])
        self.app.feedback.tell.assert_called_once()
        self.assertIn("uncertain",self.app.feedback.tell.call_args.args[0].lower())

    def test_absent_confidence_requires_clarification(self):
        self.app.cactus.route.return_value={"function_calls":[
           {"name":"create_reminder","arguments":{"message":"oops","delay_minutes":1}}
        ],"confidence":None}
        self.app.handle_text("Remind me")
        self.assertEqual(self.app.store.list_reminders(),[])

    def test_delegation_uses_original_transcript_not_model_argument(self):
        self.app.cactus.route.return_value={"function_calls":[
            {"name":"delegate_to_reasoner","arguments":{"request":"stop all services"}}
        ],"confidence":0.94}
        self.app.system2.reason=Mock(return_value={"message":"Understood","results":[]})
        self.app.handle_text("Explain what GPU memory means")
        self.app.system2.reason.assert_called_once_with(
            "Explain what GPU memory means",allow_actions=True)

    def test_empty_needle_call_invokes_read_only_qwen(self):
        self.app.cactus.route.return_value={"function_calls":[],"confidence":0.99}
        self.app.system2.reason=Mock(return_value={"message":"Answer","results":[]})
        self.app.handle_text("What is quantum computing?")
        self.app.system2.reason.assert_called_once_with(
            "What is quantum computing?",allow_actions=False)

if __name__=="__main__":
    unittest.main()
