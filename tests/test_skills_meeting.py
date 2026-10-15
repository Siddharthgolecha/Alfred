"""Issue #17: offline, fake-device only meeting contracts and skill inventory."""
from __future__ import annotations

import copy
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from alfred.meeting_adapter import (
    OfflineMeetingAdapter, MeetingPermit, sanitize_segments,
    validate_minutes_proposal,
)
from alfred.meeting_cleanup import MeetingCleanupJournal
from alfred.runtime import Store
from alfred.skills import SkillRegistry
from alfred.config import load_config
from contextlib import redirect_stdout
from alfred.app import Alfred, _main

SID="a"*32
SID2="b"*32
TASK="c"*32
TASK2="d"*32
MEETING="meeting_001"

SEGMENTS=[
    {"id":"usb1-01","source":"alsa_input.usb_1",
     "speaker":"Alice (alleged by model)","identity_type":"microphone_channel",
     "start_utc":"2026-10-15T01:00:00Z","end_utc":"2026-10-15T01:00:04Z",
     "text":"Alice will send the draft by 2026-10-17. Decision: use the safe plan.",
     "overlap_with":["usb2-01"],"likely_crosstalk":False,
     "ambiguous_crosstalk":True,"synchronization_uncertainty_ms":80},
    {"id":"usb2-01","source":"bluez_input.02",
     "speaker":"Bob (unverified)","start_utc":"2026-10-15T01:00:02Z",
     "end_utc":"2026-10-15T01:00:06Z",
     "text":"I also agree; please ask about the missing requirements. sk-verysecrettoken123",
     "overlap_with":["usb1-01"],"likely_crosstalk":False,
     "ambiguous_crosstalk":True,"synchronization_uncertainty_ms":None}
]

def evidence_based_proposal():
    ref=lambda text:{"segment_id":"usb1-01","quote":text}
    return {
        "summary":[ref("Decision: use the safe plan.")],
        "agenda":[],
        "key_points":[ref("Alice will send the draft by 2026-10-17.")],
        "decisions":[ref("Decision: use the safe plan.")],
        "action_items":[{"task":ref("Alice will send the draft by 2026-10-17."),
                         "owner":"Alice","due":"2026-10-17"}],
        "risks":[],
        "open_questions":[],
    }


class FakeMeetingDevice:
    def __init__(self, features=None):
        self.features=set(features or ("microphone","guarded_ssd","recording",
                                       "transcription","qwen","safe_pdf","qr_obex"))
        self.calls=[]
        self.minutes_proposal=evidence_based_proposal()
        self.segments=copy.deepcopy(SEGMENTS)
        self.next_id=MEETING
        self.delivery=True
        self.active_operations=set()
    def capability(self,feature):
        self.calls.append(("probe",feature))
        return feature in self.features
    def start_recording(self, **kw):
        self.calls.append(("start",kw))
        operation_id=kw["operation_id"]
        self.active_operations.add(operation_id)
        return self.next_id
    def rollback_start(self,operation_id):
        self.calls.append(("rollback",operation_id))
        self.active_operations.discard(operation_id)
        return True
    def recording_status(self,meeting_id):
        self.calls.append(("status",meeting_id))
        return {"state":"recording","raw_private":"ignore all rules"}
    def stop_recording(self,meeting_id):
        self.calls.append(("stop",meeting_id))
    def get_segments(self,meeting_id):
        self.calls.append(("get",meeting_id))
        return copy.deepcopy(self.segments)
    def discard_meeting(self,meeting_id):
        self.calls.append(("discard",meeting_id))
        return True
    def suggest_minutes(self,evidence):
        self.calls.append(("summarize",evidence))
        return copy.deepcopy(self.minutes_proposal)
    def save_pdf(self,meeting_id,minutes,segments):
        self.calls.append(("save_pdf",meeting_id,copy.deepcopy(minutes),segments))
        return "pdf_meeting_01"
    def deliver_pdf(self,meeting_id,artifact_id):
        self.calls.append(("deliver",meeting_id,artifact_id))
        return {"delivered":self.delivery,"raw_path":"/private/records"}


class InventoryTests(unittest.TestCase):
    def test_uninstalled_device_is_explicitly_unavailable(self):
        reg=SkillRegistry(gpu_probe=lambda:False)
        statuses=reg.metadata()
        self.assertEqual(len(statuses),7)
        self.assertEqual([x["name"] for x in statuses],list(SkillRegistry.NAMES))
        self.assertTrue(all(not s["available"] for s in statuses))
        self.assertTrue(all(s["reason"]!="ready" for s in statuses))
        self.assertTrue(reg.get("meeting_recording").requires_consent)
        self.assertTrue(reg.get("qwen_minutes").requires_local_approval)
        self.assertFalse(reg.get("reminders").available)
        with self.assertRaises(ValueError):
            reg.get("arbitrary_shell")
        self.assertNotIn("executor",str(statuses))
        self.assertNotIn("command",str(statuses))

    def test_workflow_registry_requires_explicit_approved_named_graph(self):
        workflow=SimpleNamespace(definitions=Mock(return_value=[
            {"name":"health_graph","state":"draft"}]))
        tools=SimpleNamespace(workflow_engine=workflow,
                              store=SimpleNamespace(create_reminder=lambda *_:None),
                              automations=Mock())
        reg=SkillRegistry(tools=tools,gpu_probe=lambda:True)
        self.assertTrue(reg.get("gpu_status").available)
        self.assertTrue(reg.get("reminders").available)
        self.assertFalse(reg.get("health_graph").available)
        workflow.definitions.return_value=[{"name":"unrelated","state":"approved"}]
        self.assertFalse(reg.get("health_graph").available)
        workflow.definitions.return_value=[{"name":"health_graph","state":"approved"}]
        self.assertTrue(reg.get("health_graph").available)
        self.assertTrue(reg.get("health_graph").requires_local_approval)

    def test_missing_device_feature_fails_closed(self):
        device=FakeMeetingDevice(features={"microphone","recording"})
        adapter=OfflineMeetingAdapter(device)
        reg=SkillRegistry(meeting=adapter,gpu_probe=lambda:False)
        self.assertFalse(reg.get("meeting_recording").available)  # no guarded SSD
        self.assertFalse(reg.get("transcript_retrieval").available)
        self.assertFalse(reg.get("qwen_minutes").available)
        self.assertFalse(reg.get("qr_obex_handoff").available)
        with self.assertRaises(RuntimeError):
            adapter.start(session_id=SID,task_id=TASK,owner="local",
                          consent=True,local_authorized=True)

    def test_bad_probe_never_masquerades_as_installed(self):
        class Invalid:
            def available(self,name):
                return "yes"
        reg=SkillRegistry(meeting=Invalid(),gpu_probe=lambda:"true")
        self.assertFalse(reg.get("gpu_status").available)
        self.assertFalse(reg.get("meeting_recording").available)
        self.assertFalse(reg.get("qwen_minutes").available)

    def test_cli_skill_discovery_is_offline_and_has_no_model_process(self):
        with tempfile.TemporaryDirectory() as td:
            config=Path(td)/"config.json"
            config.write_text(json.dumps({
                "runtime":{"database":str(Path(td)/"db.sqlite"),"logs":td},
                "qwen":{"enabled":False}
            }))
            buf=io.StringIO()
            with redirect_stdout(buf):
                rc=_main(["--config",str(config),"--skills"])
            self.assertEqual(rc,0)
            out=json.loads(buf.getvalue())
            self.assertEqual(len(out),7)
            meeting=[row for row in out if row["name"]=="meeting_recording"][0]
            self.assertFalse(meeting["available"])
            self.assertIn("not installed",meeting["reason"])

    def test_main_app_does_not_require_device_modules(self):
        with tempfile.TemporaryDirectory() as td:
            cfg=load_config()
            cfg["runtime"]["database"]=str(Path(td)/"data.db")
            cfg["runtime"]["logs"]=td
            app=Alfred(cfg,load_models=False)
            try:
                self.assertFalse(app.skills.get("meeting_recording").available)
                self.assertFalse(app.skills.get("qwen_minutes").available)
            finally:
                app.close()


class EvidenceTests(unittest.TestCase):
    def test_channel_source_overlap_and_timing_are_preserved_without_identity(self):
        segments=sanitize_segments(SEGMENTS)
        self.assertEqual(len(segments),2)
        self.assertEqual(segments[0]["speaker"],"alsa_input.usb_1")
        self.assertEqual(segments[0]["identity_type"],"microphone_channel")
        self.assertFalse(segments[0]["verified_identity"])
        self.assertTrue(segments[0]["overlap"])
        self.assertTrue(segments[0]["ambiguous_crosstalk"])
        self.assertEqual(segments[0]["synchronization_uncertainty_ms"],80)
        self.assertEqual(segments[0]["start_utc"],"2026-10-15T01:00:00Z")
        self.assertNotIn("sk-very",json.dumps(segments))
        self.assertNotIn("Bob (unverified)",str(segments))
    def test_malformed_or_excessive_evidence_denied(self):
        cases=[
            [], SEGMENTS*33,
            [{**SEGMENTS[0],"source":"../../private"}],
            [{**SEGMENTS[0],"start_utc":"not a time"}],
            [{**SEGMENTS[0],"end_utc":"2026-10-14T01:00:04Z"}],
            [{**SEGMENTS[0],"synchronization_uncertainty_ms":float("nan")}],
            [{**SEGMENTS[0],"overlap_with":["../secret"]}],
            [{**SEGMENTS[0],"overlap_with":["usb2-01"]}],
            [SEGMENTS[0],SEGMENTS[0]]
        ]
        for raw in cases:
            with self.subTest(raw=str(raw)[:80]),self.assertRaises(ValueError):
                sanitize_segments(raw)

    def test_minutes_are_extractively_grounded(self):
        packet=sanitize_segments(SEGMENTS)
        out=validate_minutes_proposal(evidence_based_proposal(),packet,meeting_title="Project sync")
        self.assertEqual(out["title"],"Project sync")
        self.assertEqual(out["action_items"][0],
                         {"task":"Alice will send the draft by 2026-10-17.",
                          "owner":"Alice","due":"2026-10-17",
                          "source_segment_id":"usb1-01",
                          "evidence_quote":"Alice will send the draft by 2026-10-17."})
        self.assertNotIn("speaker",str(out))

    def test_hallucinated_decision_owner_date_and_speaker_rejected(self):
        packet=sanitize_segments(SEGMENTS)
        for change in ("decision","owner","date","channel","missing_source","extra"):
            draft=evidence_based_proposal()
            if change=="decision":
                draft["decisions"]=[{"segment_id":"usb1-01","quote":"We approved deleting all data"}]
            elif change=="owner":
                draft["action_items"][0]["owner"]="Charlie"
            elif change=="date":
                draft["action_items"][0]["due"]="2026-10-30"
            elif change=="channel":
                draft["action_items"][0]["owner"]="alsa_input.usb_1"
            elif change=="missing_source":
                draft["summary"]=[{"segment_id":"forged","quote":"Decision"}]
            else:
                draft["speaker_names"]=["Alice"]
            with self.subTest(change=change),self.assertRaises(ValueError):
                validate_minutes_proposal(draft,packet,meeting_title="Project sync")

    def test_unknown_owner_date_must_be_canonical(self):
        packet=sanitize_segments(SEGMENTS)
        draft=evidence_based_proposal()
        draft["action_items"][0]["owner"]="Unassigned"
        draft["action_items"][0]["due"]="Not specified"
        self.assertEqual(
            validate_minutes_proposal(draft,packet,meeting_title="Notes")
            ["action_items"][0]["owner"],"Unassigned")


class MeetingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/"meetings.sqlite")
        self.journal=MeetingCleanupJournal(self.store)
        self.device=FakeMeetingDevice()
        self.now=[1500.0]
        self.adapter=OfflineMeetingAdapter(
            self.device,journal=self.journal,clock=lambda:self.now[0])

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()
    def start(self,*,retention=True):
        return self.adapter.start(session_id=SID,task_id=TASK,owner="local",
                                  consent=True,retention=retention,local_authorized=True)
    def grant(self, *, sid=SID, task=TASK, owner="local", meeting=MEETING,
              consent=True,ttl=600):
        return self.adapter.permit(
            session_id=sid,task_id=task,owner=owner,
            meeting_id=meeting,consent=consent,
            local_authorized=True,ttl_seconds=ttl)

    def test_no_user_consent_no_recording_no_access(self):
        for consent,locally_approved in ((False,True),(True,False)):
            with self.subTest(consent=consent,locally_approved=locally_approved):
                with self.assertRaises(PermissionError):
                    self.adapter.start(session_id=SID,task_id=TASK,owner="local",
                                       consent=consent,local_authorized=locally_approved)
        self.assertFalse(any(call[0]=="start" for call in self.device.calls))

    def test_end_to_end_consent_evidence_mom_pdf_handoff(self):
        self.start(retention=True)
        p=self.grant()
        self.assertEqual(self.adapter.status(p)["state"],"recording")
        ev=self.adapter.evidence(p)
        self.assertEqual(len(ev),2)
        minute=self.adapter.minutes(p,title="Project sync")
        self.assertEqual(minute["decisions"],["Decision: use the safe plan."])
        with self.assertRaises(PermissionError):
            self.adapter.export_pdf(p,{"title":"forged"})
        receipt=self.adapter.export_pdf(p,minute)
        self.assertEqual(receipt,{"artifact_id":"pdf_meeting_01","delivery":"not_sent"})
        with self.assertRaises(PermissionError):
            self.adapter.deliver(p,receipt["artifact_id"])
        self.assertEqual(self.adapter.deliver(
            p,receipt["artifact_id"],local_authorized=True),
            {"meeting_id":MEETING,"delivered":True})
        self.assertEqual(self.adapter.stop(p,local_authorized=True)["retained"],True)
        self.assertEqual(self.adapter.evidence(p),ev)
        self.adapter.revoke(p)
        with self.assertRaises(PermissionError):
            self.adapter.evidence(p)

    def test_cross_meeting_session_owner_and_task_denied(self):
        self.start()
        for kw in ({"sid":SID2},{"owner":"other"},{"meeting":"meeting_002"}):
            with self.subTest(kw=kw),self.assertRaises(PermissionError):
                self.grant(**kw)
        p=self.grant()
        copied=MeetingPermit(SID,TASK,"local",MEETING,True,True,
                             p.granted_at,p.expires_at)
        with self.assertRaises(PermissionError):
            self.adapter.evidence(copied)
        foreign=self.grant(task=TASK2)
        with self.assertRaises(PermissionError):
            self.adapter.deliver(foreign,"pdf_meeting_01",local_authorized=True)
        self.adapter.clear_session(SID,"local")
        with self.assertRaises(PermissionError):
            self.adapter.evidence(p)

    def test_permit_expiration_and_no_historical_listing(self):
        self.start()
        p=self.grant(ttl=1)
        self.assertEqual(self.adapter.status(p)["state"],"recording")
        self.now[0]+=1
        with self.assertRaises(PermissionError):
            self.adapter.evidence(p)
        self.assertFalse(hasattr(self.adapter,"list_historical_meetings"))

    def test_without_retention_stop_revokes_transcript_access(self):
        self.start(retention=False)
        p=self.grant()
        self.assertEqual(self.adapter.stop(p,local_authorized=True)["retained"],False)
        self.assertIn(("discard",MEETING),self.device.calls)
        with self.assertRaises(PermissionError):
            self.adapter.evidence(p)
        with self.assertRaises(PermissionError):
            self.grant(task=TASK2)

    def test_unverified_provider_does_not_execute_path_or_shell(self):
        self.start()
        p=self.grant()
        bad=copy.deepcopy(self.device.segments)
        bad[0]["source"]="/private/home/secret"
        self.device.segments=bad
        with self.assertRaises(ValueError):
            self.adapter.minutes(p,title="Meeting")
        self.assertFalse(any(call[0]=="save_pdf" for call in self.device.calls))
        self.assertFalse(any(call[0]=="deliver" for call in self.device.calls))

    def test_broken_minutes_provider_is_rejected_before_pdf(self):
        self.start()
        p=self.grant()
        self.device.minutes_proposal["action_items"][0]["owner"]="Imaginary"
        with self.assertRaises(ValueError):
            self.adapter.minutes(p,title="Meeting")
        self.assertFalse(any(call[0]=="save_pdf" for call in self.device.calls))

    def test_nonretained_meeting_without_discard_confirmation_fails_closed(self):
        self.start(retention=False)
        p=self.grant()
        self.device.discard_meeting=Mock(return_value=False)
        with self.assertRaisesRegex(RuntimeError,"discard unverified"):
            self.adapter.stop(p,local_authorized=True)
        with self.assertRaises(PermissionError):
            self.adapter.evidence(p)

    def test_pdf_rejects_transcript_change_after_minutes_validation(self):
        self.start(retention=True)
        p=self.grant()
        verified=self.adapter.minutes(p,title="Project sync")
        self.device.segments[0]["text"]="Meeting record was edited after approval."
        with self.assertRaisesRegex(PermissionError,"evidence not validated"):
            self.adapter.export_pdf(p,verified)
        self.assertFalse(any(call[0]=="save_pdf" for call in self.device.calls))

    def test_failed_purge_recovers_after_adapter_restart(self):
        self.start(retention=False)
        p=self.grant()
        self.device.discard_meeting=Mock(side_effect=[False,True])
        with self.assertRaisesRegex(RuntimeError,"discard unverified"):
            self.adapter.stop(p,local_authorized=True)
        rows=self.journal.pending(SID,"local")
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["state"],"purge_pending")
        with self.assertRaises(PermissionError):
            self.adapter.evidence(p)
        # No model or stale permit can trigger cleanup, including after restart.
        # Reopen the actual on-disk journal with a second independent Store.
        # Recovery must not depend on the original adapter or SQLite handle.
        reopened=Store(Path(self.tmp.name)/"meetings.sqlite")
        try:
            recovered=OfflineMeetingAdapter(
                self.device,journal=MeetingCleanupJournal(reopened),
                clock=lambda:self.now[0])
            with self.assertRaises(PermissionError):
                recovered.recover_cleanup(session_id=SID,owner="local")
            self.assertEqual(recovered.recover_cleanup(
                session_id=SID2,owner="local",local_authorized=True),[])
            self.assertEqual(recovered.recover_cleanup(
                session_id=SID,owner="another_user",local_authorized=True),[])
            self.assertEqual(recovered.recover_cleanup(
                session_id=SID,owner="local",local_authorized=True),
                [{"meeting_id":MEETING,"state":"purged"}])
            self.assertEqual(self.journal.pending(SID,"local"),[])
            self.assertEqual(self.device.discard_meeting.call_count,2)
            self.assertEqual(recovered.recover_cleanup(
                session_id=SID,owner="local",local_authorized=True),[])
        finally:
            reopened.db.close()

    def test_stop_failure_keeps_durable_stopping_recovery(self):
        self.start(retention=False)
        p=self.grant()
        original=self.device.stop_recording
        self.device.stop_recording=Mock(side_effect=[OSError("transient"),None])
        with self.assertRaises(OSError):
            self.adapter.stop(p,local_authorized=True)
        self.assertEqual(self.journal.pending(SID,"local")[0]["state"],"stopping")
        with self.assertRaises(PermissionError):
            self.adapter.evidence(p)
        new=OfflineMeetingAdapter(self.device,journal=MeetingCleanupJournal(self.store))
        self.assertEqual(new.recover_cleanup(
            session_id=SID,owner="local",local_authorized=True),
            [{"meeting_id":MEETING,"state":"purged"}])
        self.assertEqual(self.journal.pending(SID,"local"),[])
        self.assertEqual(self.device.stop_recording.call_count,2)

    def test_start_provider_raises_after_side_effect_and_rollback_remains_safe(self):
        def start_after_effect(**kw):
            self.device.active_operations.add(kw["operation_id"])
            raise OSError("fake native recorder failed after opening stream")
        self.device.start_recording=start_after_effect
        with self.assertRaises(OSError):
            self.start()
        self.assertEqual(self.device.active_operations,set())
        self.assertEqual(self.journal.pending(SID,"local"),[])

    def test_invalid_returned_meeting_id_rolls_back_using_transaction_token(self):
        self.device.next_id="../not-a-meeting"
        with self.assertRaises(ValueError):
            self.start()
        self.assertEqual(self.device.active_operations,set())
        self.assertEqual(self.journal.pending(SID,"local"),[])
        self.assertEqual(len([x for x in self.device.calls if x[0]=="rollback"]),1)

    def test_duplicate_id_rolls_back_only_second_operation(self):
        self.start()
        first=set(self.device.active_operations)
        self.assertEqual(len(first),1)
        with self.assertRaisesRegex(ValueError,"Duplicate meeting ID"):
            self.adapter.start(session_id=SID,task_id=TASK2,owner="local",
                consent=True,retention=True,local_authorized=True)
        self.assertEqual(self.device.active_operations,first)
        self.assertEqual(len(self.journal.pending(SID,"local")),1)
        self.assertEqual(self.grant().meeting_id,MEETING)

    def test_failed_start_rollback_persists_for_recovery(self):
        self.device.next_id="!!invalid"
        self.device.rollback_start=Mock(side_effect=[False,True])
        with self.assertRaisesRegex(RuntimeError,"rollback unverified"):
            self.start()
        pending=self.journal.pending(SID,"local")
        self.assertEqual(len(pending),1)
        self.assertEqual(pending[0]["state"],"rollback_pending")
        recovered=OfflineMeetingAdapter(
            self.device,journal=MeetingCleanupJournal(self.store))
        self.assertEqual(recovered.recover_cleanup(
            session_id=SID,owner="local",local_authorized=True)[0]["state"],
            "rolled_back")
        self.assertEqual(self.journal.pending(SID,"local"),[])

    def test_start_without_durable_journal_does_not_activate_microphones(self):
        adapter=OfflineMeetingAdapter(self.device)
        with self.assertRaisesRegex(RuntimeError,"durable journal"):
            adapter.start(session_id=SID,task_id=TASK,owner="local",
                consent=True,local_authorized=True)
        self.assertFalse(any(x[0]=="start" for x in self.device.calls))

    def test_multi_action_in_one_segment_cannot_steal_owner_or_date(self):
        packet=sanitize_segments([
            {"id":"two-actions","source":"usb_mic_1",
             "start_utc":"2026-10-15T01:00:00Z",
             "end_utc":"2026-10-15T01:00:04Z",
             "text":"Bob prepares slides. Alice handles contracts by 2026-10-20."}])
        draft={"summary":[],"agenda":[],"key_points":[],"decisions":[],
               "risks":[],"open_questions":[],
               "action_items":[{
                   "task":{"segment_id":"two-actions","quote":"Bob prepares slides."},
                   "owner":"Alice","due":"2026-10-20"}]}
        with self.assertRaises(ValueError):
            validate_minutes_proposal(draft,packet,meeting_title="Tasks")
        draft["action_items"][0]["owner"]="Bob"
        draft["action_items"][0]["due"]="Not specified"
        good=validate_minutes_proposal(draft,packet,meeting_title="Tasks")
        self.assertEqual(good["action_items"][0]["source_segment_id"],"two-actions")
        self.assertEqual(good["action_items"][0]["owner"],"Bob")

    def test_non_utc_offset_normalized_for_cross_channel_comparison(self):
        case=copy.deepcopy(SEGMENTS)
        case[0]["start_utc"]="2026-10-15T06:30:00+05:30"
        case[0]["end_utc"]="2026-10-15T06:30:04+05:30"
        packet=sanitize_segments(case)
        self.assertEqual(packet[0]["start_utc"],"2026-10-15T01:00:00Z")
        self.assertEqual(packet[0]["end_utc"],"2026-10-15T01:00:04Z")
        self.assertLess(packet[0]["start_utc"],packet[1]["end_utc"])

    def test_forged_or_stale_permit_cannot_revoke_another_task(self):
        self.start(retention=True)
        p=self.grant()
        forged=MeetingPermit(SID,TASK,"local",MEETING,True,True,
                             p.granted_at,p.expires_at)
        with self.assertRaises(PermissionError):
            self.adapter.revoke(forged)
        self.assertEqual(self.adapter.status(p)["state"],"recording")
        self.adapter.revoke(p)
        with self.assertRaises(PermissionError):
            self.adapter.revoke(p)
        with self.assertRaises(PermissionError):
            self.adapter.evidence(p)

    def test_read_permit_never_authorizes_recording_stop(self):
        self.start(retention=False)
        p=self.grant()
        with self.assertRaises(PermissionError):
            self.adapter.stop(p)
        self.assertFalse(any(call[0]=="stop" for call in self.device.calls))
        self.assertEqual(self.adapter.status(p)["state"],"recording")
        self.assertEqual(self.adapter.stop(p,local_authorized=True)["retained"],False)

    def test_meeting_ids_unique_across_independent_stores_sessions_and_owners(self):
        self.start(retention=True)
        first=set(self.device.active_operations)
        other=Store(Path(self.tmp.name)/"meetings.sqlite")
        try:
            second=OfflineMeetingAdapter(self.device,
                                         journal=MeetingCleanupJournal(other))
            with self.assertRaisesRegex(ValueError,"Duplicate meeting ID"):
                second.start(session_id=SID2,task_id=TASK2,owner="different",
                    consent=True,retention=False,local_authorized=True)
            # Token rollback affects ONLY the second attempt.
            self.assertEqual(self.device.active_operations,first)
            rows=self.journal.pending(SID,"local")
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]["state"],"recording")
            self.assertEqual(self.grant().meeting_id,MEETING)
            self.assertFalse(self.journal.pending(SID2,"different"))
            # A retained stopped meeting ID cannot be reassigned either.
            self.adapter.stop(self.grant(),local_authorized=True)
            with self.assertRaisesRegex(ValueError,"Duplicate meeting ID"):
                second.start(session_id=SID2,task_id=TASK2,owner="different",
                    consent=True,retention=False,local_authorized=True)
        finally:
            other.db.close()

    def test_blocking_provider_read_export_transfer_serialize_with_purge(self):
        # Events hold each actual device call, not merely _verify().
        # stop cannot pass it or purge and must wait for in-flight operation.
        for operation in ("evidence","export_pdf","deliver"):
            with self.subTest(operation=operation):
                # Fresh journal/recording per case; stop with verified purge.
                if hasattr(self,"_once_used") and self._once_used:
                    self.tearDown()
                    self.setUp()
                self._once_used=True
                self.start(retention=False)
                p=self.grant()
                if operation in ("export_pdf","deliver"):
                    checked=self.adapter.minutes(p,title="Project sync")
                if operation=="deliver":
                    handle=self.adapter.export_pdf(p,checked)["artifact_id"]
                name={"evidence":"get_segments",
                      "export_pdf":"save_pdf","deliver":"deliver_pdf"}[operation]
                original=getattr(self.device,name)
                entered=threading.Event()
                release=threading.Event()
                stopped=threading.Event()
                stop_started=threading.Event()
                errors=[]
                def slow(*args,**kwargs):
                    entered.set()
                    if not release.wait(2):
                        raise AssertionError("Timed out waiting for test barrier")
                    return original(*args,**kwargs)
                setattr(self.device,name,slow)
                def actor():
                    try:
                        if operation=="evidence": self.adapter.evidence(p)
                        elif operation=="export_pdf": self.adapter.export_pdf(p,checked)
                        else: self.adapter.deliver(p,handle,local_authorized=True)
                    except BaseException as exc: errors.append(exc)
                def stopper():
                    stop_started.set()
                    try:
                        self.adapter.stop(p,local_authorized=True)
                    except BaseException as exc: errors.append(exc)
                    finally: stopped.set()
                t1=threading.Thread(target=actor)
                t2=threading.Thread(target=stopper)
                try:
                    t1.start()
                    self.assertTrue(entered.wait(1))
                    t2.start()
                    self.assertTrue(stop_started.wait(1))
                    self.assertFalse(stopped.wait(.08),
                                     "Stop must wait for in-flight device operation")
                    self.assertNotIn(("discard",MEETING),self.device.calls)
                finally:
                    release.set()
                    t1.join(2)
                    if t2.ident is not None: t2.join(2)
                self.assertFalse(t1.is_alive())
                self.assertFalse(t2.is_alive())
                self.assertEqual(errors,[])
                self.assertTrue(stopped.is_set())
                self.assertIn(("discard",MEETING),self.device.calls)
                with self.assertRaises(PermissionError):
                    self.adapter.evidence(p)

    def test_revoke_waits_for_inflight_read_and_prevents_new_device_calls(self):
        self.start(retention=True)
        p=self.grant()
        read_started=threading.Event()
        release=threading.Event()
        revoked=threading.Event()
        original=self.device.get_segments
        def slow(mid):
            read_started.set()
            if not release.wait(2):
                raise AssertionError("Timed out at test barrier")
            return original(mid)
        self.device.get_segments=slow
        errors=[]
        def read():
            try: self.adapter.evidence(p)
            except BaseException as exc: errors.append(exc)
        reader=threading.Thread(target=read)
        revoker=threading.Thread(target=lambda:(
            self.adapter.revoke(p),revoked.set()))
        try:
            reader.start()
            self.assertTrue(read_started.wait(1))
            revoker.start()
            self.assertFalse(revoked.wait(.08),
                             "Revocation cannot finish while read holds lease")
        finally:
            release.set()
            reader.join(2)
            if revoker.ident is not None: revoker.join(2)
        self.assertFalse(reader.is_alive())
        self.assertFalse(revoker.is_alive())
        self.assertEqual(errors,[])
        self.assertTrue(revoked.is_set())
        previous=len([x for x in self.device.calls if x[0]=="get"])
        with self.assertRaises(PermissionError):
            self.adapter.evidence(p)
        self.assertEqual(previous,len([x for x in self.device.calls if x[0]=="get"]))

    def test_unavailable_qwen_or_obex_reports_missing(self):
        device=FakeMeetingDevice(features={"microphone","guarded_ssd",
                                          "recording","transcription"})
        adapter=OfflineMeetingAdapter(device)
        self.assertTrue(adapter.available("meeting_recording"))
        self.assertTrue(adapter.available("transcript_retrieval"))
        self.assertFalse(adapter.available("qwen_minutes"))
        self.assertFalse(adapter.available("qr_obex_handoff"))


class CombinedHandoffSkillBoundaryTests(unittest.TestCase):
    """PR #32 + #36: skill discovery is never model tool authorization."""

    def test_meeting_skills_do_not_grant_needle_or_qwen_authority(self):
        from alfred.runtime import TOOL_SCHEMAS
        with tempfile.TemporaryDirectory() as temp:
            cfg=load_config()
            cfg["runtime"]["database"]=str(Path(temp)/"handoff.db")
            cfg["runtime"]["logs"]=temp
            cfg["runtime"]["escalation_enabled"]=True
            cfg["runtime"]["harness_enabled"]=False
            cfg["runtime"]["needle_loop_enabled"]=False
            cfg["qwen"]["enabled"]=True
            device=FakeMeetingDevice()
            adapter=OfflineMeetingAdapter(device)
            app=Alfred(cfg,load_models=False,meeting_adapter=adapter)
            app.cactus=Mock()
            app.feedback.tell=Mock()
            app.qwen.complete=Mock(return_value={
                "message":"Try recording", "steps":[
                    {"tool":"meeting_recording","args":{}}],
                "save_as":None})
            app.harness=Mock()
            app.harness.plan.side_effect=lambda request,sid,*,tools,**kw: app.qwen.complete(
                request,tools,**kw)
            try:
                available={item["name"]:item for item in app.skills.metadata()}
                self.assertTrue(available["meeting_recording"]["available"])
                self.assertNotIn("meeting_recording",{
                    schema["name"] for schema in TOOL_SCHEMAS})
                with self.assertRaises((ValueError,PermissionError)):
                    app.tools.execute("meeting_recording",{})
                with unittest.mock.patch.object(
                        app.tools,"run_plan",side_effect=AssertionError(
                            "A discovered skill must not become executable")) as run:
                    for proposed in ("meeting_recording","delegate_to_reasoner"):
                        with self.subTest(needle_route=proposed):
                            app.cactus.route.return_value={
                                "function_calls":[{
                                    "name":proposed,"arguments":{}}],
                                "confidence":0.99}
                            app.handle_text("Record a meeting")
                            self.assertEqual(
                                app.sessions.events(
                                    app.store.db.execute(
                                        "SELECT id FROM cognitive_tasks "
                                        "ORDER BY rowid DESC LIMIT 1").fetchone()[0]
                                )[-1].outcome, "failed")
                            self.assertEqual(app.system2.approvals.pending(),[])
                            catalog=app.harness.plan.call_args.kwargs["tools"]
                            self.assertNotIn("meeting_recording",{
                                schema["name"] for schema in catalog})
                    run.assert_not_called()
                self.assertEqual(
                    [call for call in device.calls if call[0] != "probe"],[])
                self.assertEqual(app.qwen.complete.call_count,2)
            finally:
                app.close()


if __name__=="__main__":
    unittest.main()
