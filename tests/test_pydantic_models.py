"""Pydantic v2 model tests for contracts, context Slot, and MeetingPermit."""
from __future__ import annotations

import time
import pytest

from pydantic import ValidationError

from alfred.cognitive.contracts import (
    TaskEnvelope, Decision, Observation, Escalation,
    ApprovalReference, Completion, Diagnostic,
    decode_contract,
)
from alfred.cognitive.context import Slot
from alfred.meeting_models import MeetingPermit


SID = "a" * 32
TID = "b" * 32


# ---------------------------------------------------------------------------
# TaskEnvelope
# ---------------------------------------------------------------------------

class TestTaskEnvelope:
    def test_valid_construction(self):
        t = TaskEnvelope(session_id=SID, task_id=TID, source="text",
                         input_chars=10, started_at=1000)
        assert t.source == "text"

    def test_invalid_source_rejected(self):
        with pytest.raises((ValidationError, ValueError)):
            TaskEnvelope(session_id=SID, task_id=TID, source="unknown",
                         input_chars=10, started_at=1000)

    def test_bool_input_chars_rejected(self):
        with pytest.raises((ValidationError, ValueError)):
            TaskEnvelope(session_id=SID, task_id=TID, source="text",
                         input_chars=True, started_at=1000)

    def test_negative_started_at_rejected(self):
        with pytest.raises((ValidationError, ValueError)):
            TaskEnvelope(session_id=SID, task_id=TID, source="voice",
                         input_chars=5, started_at=-1)

    def test_invalid_session_id_rejected(self):
        with pytest.raises((ValidationError, ValueError)):
            TaskEnvelope(session_id="bad", task_id=TID, source="text",
                         input_chars=1, started_at=1)

    def test_frozen(self):
        t = TaskEnvelope(session_id=SID, task_id=TID, source="text",
                         input_chars=1, started_at=1)
        with pytest.raises((ValidationError, TypeError)):
            t.source = "voice"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

class TestDecision:
    def test_valid_with_confidence(self):
        d = Decision(engine="needle3", route="direct", call_count=2, confidence=0.9)
        assert d.confidence == pytest.approx(0.9)

    def test_valid_without_confidence(self):
        d = Decision(engine="local", route="shutdown", call_count=0)
        assert d.confidence is None

    def test_invalid_engine_rejected(self):
        with pytest.raises((ValidationError, ValueError)):
            Decision(engine="evil", route="direct", call_count=0)

    def test_invalid_route_rejected(self):
        with pytest.raises((ValidationError, ValueError)):
            Decision(engine="needle3", route="unknown", call_count=0)

    def test_bool_call_count_rejected(self):
        with pytest.raises((ValidationError, ValueError)):
            Decision(engine="needle3", route="direct", call_count=True)

    def test_nan_confidence_rejected(self):
        with pytest.raises((ValidationError, ValueError)):
            Decision(engine="needle3", route="direct", call_count=0,
                     confidence=float("nan"))


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------

class TestObservation:
    def test_valid(self):
        o = Observation(tool="get_gpu_status", outcome="returned")
        assert o.tool == "get_gpu_status"

    def test_invalid_tool_name_rejected(self):
        with pytest.raises((ValidationError, ValueError)):
            Observation(tool="bad!tool", outcome="returned")

    def test_invalid_outcome_rejected(self):
        with pytest.raises((ValidationError, ValueError)):
            Observation(tool="get_gpu_status", outcome="unknown")


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------

class TestEscalation:
    def test_valid(self):
        e = Escalation(source_model="needle3", target_model="qwen", reason="delegated")
        assert e.target_model == "qwen"

    def test_invalid_target_rejected(self):
        with pytest.raises((ValidationError, ValueError)):
            Escalation(source_model="needle3", target_model="needle3", reason="delegated")

    def test_invalid_reason_rejected(self):
        with pytest.raises((ValidationError, ValueError)):
            Escalation(source_model="needle3", target_model="qwen", reason="made_up")


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------

class TestCompletion:
    def test_valid(self):
        c = Completion(outcome="completed", reason="executed")
        assert c.outcome == "completed"

    def test_invalid_outcome_rejected(self):
        with pytest.raises((ValidationError, ValueError)):
            Completion(outcome="nevermind", reason="executed")

    def test_invalid_reason_rejected(self):
        with pytest.raises((ValidationError, ValueError)):
            Completion(outcome="completed", reason="made_up")


# ---------------------------------------------------------------------------
# ApprovalReference
# ---------------------------------------------------------------------------

class TestApprovalReference:
    def test_valid(self):
        a = ApprovalReference(approval_id=42)
        assert a.approval_id == 42

    def test_zero_rejected(self):
        with pytest.raises((ValidationError, ValueError)):
            ApprovalReference(approval_id=0)

    def test_bool_rejected(self):
        with pytest.raises((ValidationError, ValueError)):
            ApprovalReference(approval_id=True)


# ---------------------------------------------------------------------------
# Diagnostic
# ---------------------------------------------------------------------------

class TestDiagnostic:
    def test_valid(self):
        d = Diagnostic(name="needle_route", metrics={"latency_ms": 50})
        assert d.name == "needle_route"

    def test_unknown_name_rejected(self):
        with pytest.raises((ValidationError, ValueError)):
            Diagnostic(name="secret_metrics", metrics={})

    def test_sensitive_key_rejected(self):
        with pytest.raises((ValidationError, ValueError)):
            Diagnostic(name="needle_route", metrics={"api_key": 1}).to_json()

    def test_too_many_keys_rejected(self):
        from alfred.constants import Limits
        with pytest.raises((ValidationError, ValueError)):
            Diagnostic(name="needle_route",
                       metrics={str(i): i for i in range(Limits.MAX_DIAGNOSTIC_METRICS + 1)})


# ---------------------------------------------------------------------------
# decode_contract round-trip
# ---------------------------------------------------------------------------

class TestDecodeContract:
    def test_roundtrip_all_types(self):
        examples = [
            TaskEnvelope(session_id=SID, task_id=TID, source="text",
                         input_chars=1, started_at=100),
            Decision(engine="needle3", route="direct", call_count=1, confidence=0.95),
            Observation(tool="get_gpu_status", outcome="returned"),
            Escalation(source_model="needle3", target_model="qwen", reason="delegated"),
            ApprovalReference(approval_id=7),
            Completion(outcome="completed", reason="executed"),
            Diagnostic(name="capture", metrics={"channels": 2}),
        ]
        for item in examples:
            assert decode_contract(item.to_json()) == item

    def test_duplicate_key_rejected(self):
        with pytest.raises(ValueError):
            decode_contract('{"kind":"decision","kind":"decision","version":1}')

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValueError):
            decode_contract('{"kind":"unknown","version":1}')


# ---------------------------------------------------------------------------
# Slot (context)
# ---------------------------------------------------------------------------

class TestSlot:
    def test_valid_gpu_slot(self):
        now = time.time()
        s = Slot(kind="gpu_temperature", metrics={"gpu_temp_c": 55.0},
                 provenance="observed", confidence=0.99,
                 observed_at=now, expires_at=now + 120, task_id=TID)
        assert s.kind == "gpu_temperature"

    def test_unknown_kind_rejected(self):
        now = time.time()
        with pytest.raises((ValidationError, ValueError)):
            Slot(kind="unknown_kind", metrics={}, provenance="observed",
                 confidence=0.9, observed_at=now, expires_at=now + 60, task_id=TID)

    def test_wrong_provenance_rejected(self):
        now = time.time()
        with pytest.raises((ValidationError, ValueError)):
            Slot(kind="gpu_temperature", metrics={}, provenance="inferred",
                 confidence=0.9, observed_at=now, expires_at=now + 60, task_id=TID)

    def test_confidence_out_of_range_rejected(self):
        now = time.time()
        with pytest.raises((ValidationError, ValueError)):
            Slot(kind="gpu_temperature", metrics={}, provenance="observed",
                 confidence=1.5, observed_at=now, expires_at=now + 60, task_id=TID)

    def test_frozen(self):
        now = time.time()
        s = Slot(kind="gpu_temperature", metrics={}, provenance="observed",
                 confidence=0.9, observed_at=now, expires_at=now + 60, task_id=TID)
        with pytest.raises((ValidationError, TypeError)):
            s.kind = "system_status"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# MeetingPermit
# ---------------------------------------------------------------------------

class TestMeetingPermit:
    def test_valid(self):
        now = time.time()
        p = MeetingPermit(session_id=SID, task_id=TID, owner="local",
                          meeting_id="ABCDEF1234567890", consent=True,
                          allow_retention=False, granted_at=now,
                          expires_at=now + 600)
        assert p.owner == "local"

    def test_no_consent_raises_permission_error(self):
        now = time.time()
        with pytest.raises((PermissionError, ValidationError, ValueError)):
            MeetingPermit(session_id=SID, task_id=TID, owner="local",
                          meeting_id="ABCDEF1234567890", consent=False,
                          allow_retention=False, granted_at=now,
                          expires_at=now + 600)

    def test_invalid_meeting_id_rejected(self):
        now = time.time()
        with pytest.raises((ValidationError, ValueError)):
            MeetingPermit(session_id=SID, task_id=TID, owner="local",
                          meeting_id="bad!", consent=True,
                          allow_retention=False, granted_at=now,
                          expires_at=now + 600)

    def test_duration_over_max_rejected(self):
        now = time.time()
        with pytest.raises((ValidationError, ValueError)):
            MeetingPermit(session_id=SID, task_id=TID, owner="local",
                          meeting_id="ABCDEF1234567890", consent=True,
                          allow_retention=False, granted_at=now,
                          expires_at=now + 7200)  # > 3600

    def test_frozen(self):
        now = time.time()
        p = MeetingPermit(session_id=SID, task_id=TID, owner="local",
                          meeting_id="ABCDEF1234567890", consent=True,
                          allow_retention=False, granted_at=now,
                          expires_at=now + 600)
        with pytest.raises((ValidationError, TypeError)):
            p.owner = "other"  # type: ignore[misc]
