"""Phase 4 graph-workflow tests; no mic, Jetson hardware or native models."""
from pathlib import Path
import json
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

from alfred.runtime import Store,Tools,Scheduler
from alfred.system2 import System2
from alfred.workflow_engine import WorkflowEngine,validate_graph,READ_ONLY


def node(ident,tool="get_system_status",args=None,depends_on=None,**extras):
    return {"id":ident,"tool":tool,"args":args or {},
            "depends_on":depends_on or [],**extras}

def graph(name="health",steps=None):
    return {"version":1,"name":name,"steps":steps or [node("system")]}


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/"state.db")
        self.tools=Tools({
            "allowed_services":["alfred-monitor.service"],
            "named_tasks":{"safe_echo":["/bin/echo","hello"]},
            "max_plan_steps":5
        },self.store)
        self.eng=self.tools.workflow_engine
        self.s2=System2(Mock(),self.tools,self.store,{"max_plan_steps":5,
            "approval_ttl_seconds":180})

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_draft_requires_local_approval(self):
        self.eng.draft(graph())
        self.assertEqual(self.eng.get("health",require_approved=False)["state"],"draft")
        with self.assertRaisesRegex(ValueError,"not locally approved"):
            self.eng.start("health")
        self.eng.approve("health")
        self.assertEqual(self.eng.get("health")["state"],"approved")

    def test_dag_fork_join_is_valid(self):
        nodes=[node("a"),node("b","get_gpu_status",depends_on=["a"]),
               node("c","get_system_status",depends_on=["a"]),
               node("d",depends_on=["b","c"])]
        valid=validate_graph(graph("diamond",nodes),self.tools)
        self.assertEqual(len(valid["steps"]),4)

    def test_cycle_forward_refs_rejected(self):
        with self.assertRaisesRegex(ValueError,"earlier"):
            validate_graph(graph(steps=[node("a",depends_on=["b"]),
                                        node("b",depends_on=["a"])]),self.tools)

    def test_unknown_tool_and_nested_workflow_rejected(self):
        for tool in ("delegate_to_reasoner","run_workflow","arbitrary_shell",
                     "create_reminder","cancel_automation"):
            with self.subTest(tool=tool),self.assertRaises(ValueError):
                validate_graph(graph(steps=[node("a",tool)]),self.tools)

    def test_allowlist_required_before_any_execution(self):
        with self.assertRaises(PermissionError):
            self.eng.draft(graph(steps=[node("a","control_service",
                {"name":"systemd-journald.service","action":"stop"})]))
        self.assertEqual(self.eng.definitions(),[])

    def test_side_effect_tools_never_retry(self):
        with self.assertRaisesRegex(ValueError,"cannot retry"):
            self.eng.draft(graph(steps=[node("a","run_named_task",{"name":"safe_echo"},
                retry={"max_attempts":3,"backoff_seconds":2},timeout_seconds=75)]))

    def test_predicate_must_reference_dependency(self):
        with self.assertRaisesRegex(ValueError,"dependency"):
            self.eng.draft(graph(steps=[node("a"),node("b",when={
                "step":"a","field":"available","op":"eq","value":True})]))

    def test_successful_run_persists_all_steps(self):
        self.eng.draft(graph(steps=[node("status"),node("repeat",depends_on=["status"])]))
        self.eng.approve("health")
        run=self.eng.start("health")["run_id"]
        self.assertEqual(self.eng.tick(max_actions=3)[0]["state"],"succeeded")
        self.eng.tick()
        finished=self.eng.details(run)
        self.assertEqual(finished["state"],"completed")
        self.assertEqual([s["state"] for s in finished["steps"]],["succeeded","succeeded"])

    def test_predicate_true_and_false_branches(self):
        spec=graph(steps=[node("temp","get_gpu_status"),
             node("hot",depends_on=["temp"],when={
                 "step":"temp","field":"gpu_temp_c","op":"gte","value":75}),
             node("cold",depends_on=["temp"],when={
                 "step":"temp","field":"gpu_temp_c","op":"lt","value":75})])
        self.eng.draft(spec)
        self.eng.approve("health")
        run=self.eng.start("health")["run_id"]
        with patch("alfred.runtime.gpu_status",return_value={"gpu_temp_c":81,"available":True}):
            self.eng.tick()
            self.eng.tick()
        details=self.eng.details(run)
        self.assertEqual(details["state"],"completed")
        states={s["step_id"]:s["state"] for s in details["steps"]}
        self.assertEqual(states,{"temp":"succeeded","hot":"succeeded","cold":"skipped"})

    def test_skipped_dependency_propagates_through_join(self):
        spec=graph(steps=[node("root","get_gpu_status"),
            node("branch",depends_on=["root"],when={
                "step":"root","field":"gpu_temp_c","op":"gt","value":90}),
            node("downstream",depends_on=["branch"])])
        self.eng.draft(spec);self.eng.approve("health")
        run=self.eng.start("health")["run_id"]
        with patch("alfred.runtime.gpu_status",return_value={"gpu_temp_c":60}):
            self.eng.tick()
            self.eng.tick()
        states=[s["state"] for s in self.eng.details(run)["steps"]]
        self.assertEqual(states,["succeeded","skipped","skipped"])

    def test_retry_only_readonly_and_bounded(self):
        self.eng.draft(graph(steps=[node("a",retry={
            "max_attempts":2,"backoff_seconds":0})]))
        self.eng.approve("health")
        run=self.eng.start("health")["run_id"]
        with patch.object(self.tools,"execute",side_effect=[
             RuntimeError("transient"),{"uptime_seconds":5}]):
            first=self.eng.tick()
            self.assertEqual(first[0]["state"],"retry_wait")
            with self.store.lock,self.store.db:
                self.store.db.execute("UPDATE workflow_step_runs SET retry_at=0 WHERE run_id=?",(run,))
            second=self.eng.tick()
        self.eng.tick()
        self.assertEqual(second[0]["state"],"succeeded")
        self.assertEqual(self.eng.details(run)["state"],"completed")
        self.assertEqual(self.eng.details(run)["steps"][0]["attempts"],2)

    def test_failure_side_effect_cannot_replay(self):
        self.eng.draft(graph(steps=[node("task","run_named_task",{"name":"safe_echo"},
                                      timeout_seconds=75)]))
        self.eng.approve("health")
        run=self.eng.start("health")["run_id"]
        with patch.object(self.tools,"execute",side_effect=RuntimeError("failed")) as call:
            self.eng.tick()
            self.eng.tick()
            self.eng.tick()
        self.assertEqual(call.call_count,1)
        self.assertEqual(self.eng.details(run)["state"],"failed")

    def test_cancel_before_run_has_no_tool_effect(self):
        self.eng.draft(graph());self.eng.approve("health")
        run=self.eng.start("health")["run_id"]
        self.assertTrue(self.eng.cancel(run)["cancelled"])
        with patch.object(self.tools,"execute") as call:
            self.eng.tick()
        call.assert_not_called()
        self.assertEqual(self.eng.details(run)["state"],"cancelled")

    def test_cancel_during_step_does_not_revert_tool(self):
        self.eng.draft(graph());self.eng.approve("health")
        run=self.eng.start("health")["run_id"]
        def completion(*args,**kwargs):
            self.eng.cancel(run)
            return {"observed":True}
        with patch.object(self.tools,"execute",side_effect=completion) as call:
            self.eng.tick()
        self.assertEqual(call.call_count,1)
        self.assertEqual(self.eng.details(run)["state"],"cancelled")
        self.assertEqual(self.eng.details(run)["steps"][0]["state"],"interrupted")

    def test_crash_recovery_requires_operator_and_no_replay(self):
        self.eng.draft(graph());self.eng.approve("health")
        run=self.eng.start("health")["run_id"]
        with self.store.lock,self.store.db:
            self.store.db.execute("UPDATE workflow_runs SET state='running' WHERE id=?",(run,))
            self.store.db.execute("UPDATE workflow_step_runs SET state='running' WHERE run_id=?",(run,))
        # Creating an independent Alfred process must not mark active work as
        # interrupted, nor replay a potentially side-effecting tool.
        new=WorkflowEngine(self.store,self.tools)
        with patch.object(self.tools,"execute") as call:
            new.tick()
        call.assert_not_called()
        self.assertEqual(new.details(run)["state"],"running")
        self.assertTrue(new.reconcile_interrupted(run)["changed"])
        self.assertFalse(new.reconcile_interrupted(run)["changed"])
        self.assertEqual(new.details(run)["state"],"interrupted")
        with patch.object(self.tools,"execute") as call:
            new.tick()
        call.assert_not_called()

    def test_approved_draft_change_invalidates_approval(self):
        self.eng.draft(graph());self.eng.approve("health")
        old=self.eng.get("health")["digest"]
        self.eng.draft(graph(steps=[node("new")]))
        self.assertNotEqual(self.eng.get("health",False)["digest"],old)
        with self.assertRaises(ValueError):
            self.eng.start("health")

    def test_voice_run_requires_phase2_local_approval(self):
        self.eng.draft(graph());self.eng.approve("health")
        response=self.s2.direct([{"name":"run_workflow","arguments":{"name":"health"}}])
        self.assertIn("approval",response)
        self.assertEqual(self.eng.list_runs(),[])
        started=self.s2.approve(response["approval"]["approval_id"])
        self.assertEqual(started["name"],"health")
        self.assertEqual(len(self.eng.list_runs()),1)
        with self.assertRaises(ValueError):
            self.s2.approve(response["approval"]["approval_id"])

    def test_voice_approval_rejects_changed_definition(self):
        self.eng.draft(graph());self.eng.approve("health")
        response=self.s2.direct([{"name":"run_workflow","arguments":{"name":"health"}}])
        self.eng.draft(graph(steps=[node("new")]))
        self.eng.approve("health")
        with self.assertRaisesRegex(ValueError,"changed"):
            self.s2.approve(response["approval"]["approval_id"])
        self.assertEqual(self.eng.list_runs(),[])

    def test_legacy_linear_workflow_still_supported(self):
        self.store.save_workflow("old",[{"tool":"get_system_status","args":{}}])
        response=self.s2.direct([{"name":"run_workflow","arguments":{"name":"old"}}])
        self.assertIn("approval",response)
        result=self.s2.approve(response["approval"]["approval_id"])
        self.assertIn("memory_mib",result[0])

    def test_scheduler_tick_runs_without_mic_or_qwen(self):
        self.eng.draft(graph());self.eng.approve("health")
        rid=self.eng.start("health")["run_id"]
        schedule=Scheduler(self.store,self.tools,lambda _:None)
        # Simulate a scheduler iteration, including its Phase4 component.
        schedule.tools.workflow_engine.tick()
        schedule.tools.workflow_engine.tick()
        self.assertEqual(self.eng.details(rid)["state"],"completed")

    def test_graph_json_does_not_allow_extra_fields(self):
        bad=graph()
        bad["python"]="print('unsafe')"
        with self.assertRaises(ValueError):
            self.eng.draft(bad)


if __name__=="__main__":
    unittest.main()
