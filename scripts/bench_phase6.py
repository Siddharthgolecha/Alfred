#!/usr/bin/env python3
"""Deterministic offline Phase 5 baseline replay for Phase 6 preregistration.

Model output and read-only tool data are synthetic. System2 authorization,
Needle routing, and the append-only session ledger are REAL merged code.
No model weights, Jetson devices, network calls, or consequential tools run.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import statistics
import tempfile
import time
import tracemalloc
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "phase6"
PARTITIONS = ("development", "heldout")
READ_TOOLS = frozenset(("get_gpu_status", "get_system_status", "list_services"))
CASE_KEYS = frozenset((
    "id", "partition", "intention", "entry_point", "capability_snapshot",
    "mandatory_procedure_ids", "allowed_tool_set", "preconditions",
    "expected_effects", "verifiable_postconditions", "phase5_stub_response",
    "baseline_expected", "expected_phase6",
))
CAPABILITY_KEYS = frozenset((
    "owner", "mode", "qwen_enabled", "harness_enabled",
    "needle_loop_enabled", "allowed_services", "named_tasks",
    "native_model", "system2_model",
))
BASELINE_KEYS = frozenset((
    "route", "outcome", "reason", "observations", "executed_reads",
    "consequential_effects",
))
VERDICTS = frozenset(("pass", "fail", "unknown"))


def _git_blob_sha1(data: bytes) -> str:
    """Hash exact frozen bytes like Git, independent of Git availability."""
    return hashlib.sha1(
        b"blob " + str(len(data)).encode("ascii") + b"\0" + data
    ).hexdigest()


def validate_case(case: dict, partition: str) -> None:
    if type(case) is not dict or set(case) != CASE_KEYS:
        raise ValueError("Unknown or missing golden case fields")
    if case["partition"] != partition or case["entry_point"] != "needle":
        raise ValueError("Wrong partition or unsupported baseline entry point")
    if (type(case["id"]) is not str or not case["id"].startswith(
            "dev_" if partition == "development" else "hold_")):
        raise ValueError("Invalid case ID or partition")
    intention = case["intention"]
    if (type(intention) is not str or not 1 <= len(intention) <= 160 or
            "://" in intention or "@" in intention):
        raise ValueError("Non-synthetic or invalid intention")
    cap = case["capability_snapshot"]
    if type(cap) is not dict or set(cap) != CAPABILITY_KEYS:
        raise ValueError("Invalid capability snapshot")
    if (cap["owner"] != "local" or cap["mode"] != "assistant" or
            cap["qwen_enabled"] is not False or cap["harness_enabled"] is not False or
            cap["native_model"] != "stub" or cap["system2_model"] != "disabled" or
            type(cap["needle_loop_enabled"]) is not bool or
            type(cap["allowed_services"]) is not list or
            not all(type(s) is str for s in cap["allowed_services"]) or
            cap["named_tasks"] != []):
        raise ValueError("Unapproved fixture capability: refuse execution")
    for field in ("mandatory_procedure_ids", "allowed_tool_set"):
        values = case[field]
        if type(values) is not list or len(set(values)) != len(values) or (
                not values and field == "mandatory_procedure_ids") or (
                not all(type(item) is str and item for item in values)):
            raise ValueError("Invalid procedure/tool list")
    for name in ("preconditions", "verifiable_postconditions"):
        checks = case[name]
        if type(checks) is not list or not checks or any(
                type(check) is not dict or set(check) != {"id", "verdict"} or
                type(check["id"]) is not str or check["verdict"] not in VERDICTS
                for check in checks):
            raise ValueError("Invalid pre/postcondition")
    effects = case["expected_effects"]
    if type(effects) is not list or any(
            type(item) is not dict or set(item) != {"tool", "kind"} or
            item["kind"] not in ("read", "write") or
            item["tool"] not in case["allowed_tool_set"] for item in effects):
        raise ValueError("Invalid expected effects")
    response = case["phase5_stub_response"]
    if (type(response) is not dict or
            not set(response) <= {"function_calls", "confidence",
                                  "validation", "suppressed_calls"} or
            "function_calls" not in response or
            type(response["function_calls"]) is not list or
            type(response.get("confidence")) not in (float, int)):
        raise ValueError("Invalid model stub")
    expected = case["baseline_expected"]
    if type(expected) is not dict or set(expected) != BASELINE_KEYS or any(
            type(expected[k]) is not list
            for k in ("observations", "executed_reads", "consequential_effects")):
        raise ValueError("Invalid baseline expectation")
    if expected["consequential_effects"]:
        raise ValueError("Baseline must never execute side effects")
    phase6 = case["expected_phase6"]
    if (type(phase6) is not dict or
            set(phase6) != {"evidence", "procedure", "goal"} or
            any(value not in VERDICTS for value in phase6.values())):
        raise ValueError("Invalid tri-state verdict")


def load_corpus(fixtures: Path = FIXTURES):
    """Fail closed on a modified frozen heldout or development partition."""
    manifest = json.loads((fixtures / "manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("version") != 1 or
            type(manifest.get("baseline_main_sha")) is not str or
            set(manifest.get("frozen_git_blob_sha1", {})) !=
            {"development.json", "heldout.json"}):
        raise ValueError("Invalid Phase 6 baseline manifest")
    cases = []
    ids = set()
    for partition in PARTITIONS:
        filename = partition + ".json"
        raw = (fixtures / filename).read_bytes()
        if _git_blob_sha1(raw) != manifest["frozen_git_blob_sha1"][filename]:
            raise ValueError("Frozen " + filename + " integrity mismatch")
        rows = json.loads(raw)
        if type(rows) is not list or len(rows) != manifest["partitions"][partition]:
            raise ValueError("Invalid partition size")
        for case in rows:
            validate_case(case, partition)
            if case["id"] in ids:
                raise ValueError("Duplicate case ID")
            ids.add(case["id"])
        cases.extend(rows)
    return manifest, cases


def _fake_read(name, args, from_plan=False):
    # The hard deny is an independent safety interlock for the test runner;
    # it must NEVER execute a proposed write even if Phase 5 routing regresses.
    if name not in READ_TOOLS or args != {}:
        raise AssertionError("Attempted non-read-only tool execution")
    if name == "get_gpu_status":
        return {"available": True, "gpu_temp_c": 62}
    if name == "get_system_status":
        return {"available": True, "uptime_seconds": 120}
    return {}


def replay(case: dict) -> dict:
    """Project actual Phase 5 ledger evidence, eliminating UUID/time/approval ID."""
    validate_case(case, case["partition"])
    # Import only when executing. Never instantiate/download the native model.
    from alfred.app import Alfred
    from alfred.config import load_config

    with tempfile.TemporaryDirectory(prefix="alfred-p6-offline-") as temp:
        cfg = load_config()
        cfg["qwen"]["enabled"] = False
        cfg["runtime"]["database"] = str(Path(temp) / "baseline.db")
        cfg["runtime"]["logs"] = temp
        cfg["runtime"]["harness_enabled"] = False
        cfg["runtime"]["context_enabled"] = False
        cfg["runtime"]["context_persistence_enabled"] = False
        cfg["runtime"]["needle_loop_enabled"] = case["capability_snapshot"]["needle_loop_enabled"]
        cfg["runtime"]["allowed_services"] = case["capability_snapshot"]["allowed_services"]
        cfg["runtime"]["named_tasks"] = {}
        app = Alfred(cfg, load_models=False)
        app.cactus = Mock()
        app.cactus.route.return_value = deepcopy(case["phase5_stub_response"])
        app.feedback.tell = Mock()
        executed = []
        def safe_tool(name, args, from_plan=False):
            result = _fake_read(name, args, from_plan)
            executed.append(name)
            return result
        try:
            with patch.object(app.tools, "execute", side_effect=safe_tool):
                app.handle_text(case["intention"])
            task_row = app.store.db.execute(
                "SELECT id FROM cognitive_tasks ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            if task_row is None:
                raise AssertionError("No task was recorded")
            events = app.sessions.events(task_row[0])
            decisions = [e for e in events if e.kind == "decision"]
            if len(decisions) != 1 or events[-1].kind != "completion":
                raise AssertionError("Missing unique decision or terminal completion")
            decision, completion = decisions[0], events[-1]
            result = {
                "id": case["id"],
                "route": decision.route,
                "outcome": completion.outcome,
                "reason": completion.reason,
                "observations": [
                    [e.tool, e.outcome] for e in events if e.kind == "observation"
                ],
                "executed_reads": executed,
                "consequential_effects": [],
            }
            if app.cactus.route.call_count != 1:
                raise AssertionError("Expected exactly one synthetic Needle inference")
            if case["capability_snapshot"]["needle_loop_enabled"] and (
                    app.cactus.route_feedback.call_count):
                raise AssertionError("Synthetic baseline unexpectedly entered feedback loop")
            return result
        finally:
            app.close()


def run(partition: str = "all", verify: bool = True) -> dict:
    manifest, cases = load_corpus()
    if partition not in (*PARTITIONS, "all"):
        raise ValueError("Unknown partition")
    selected = [case for case in cases if partition in ("all", case["partition"])]
    observed = []
    for case in selected:
        actual = replay(case)
        expected = {"id": case["id"], **case["baseline_expected"]}
        if verify and actual != expected:
            raise AssertionError(
                f"Phase 5 drift: {case['id']}: expected {expected!r}; got {actual!r}")
        observed.append(actual)
    return {
        "schema_version": 1,
        "source_baseline_sha": manifest["baseline_main_sha"],
        "evidence": "synthetic Needle + fake read-only tools; real merged Python routing/ledger",
        "partition": partition,
        "verified_against_frozen_expectations": verify,
        "cases": observed,
        "native_jetson": "not_measured",
    }


def measure(samples: int = 5) -> dict:
    """Optional same-host Python-path timing; NOT hardware/model latency."""
    if type(samples) is not int or not 2 <= samples <= 50:
        raise ValueError("samples must be 2..50")
    timings = []
    peaks = []
    for _ in range(samples):
        tracemalloc.start()
        begin = time.perf_counter_ns()
        run()
        timings.append((time.perf_counter_ns() - begin) / 1_000_000)
        _, peak = tracemalloc.get_traced_memory()
        peaks.append(peak)
        tracemalloc.stop()
    ordered = sorted(timings)
    return {
        "host_mock_full_corpus_p50_ms": round(statistics.median(ordered), 3),
        "host_mock_full_corpus_p95_ms": round(
            ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)], 3),
        "host_mock_peak_tracemalloc_bytes": max(peaks),
        "samples": samples,
        "native_jetson": "not_measured",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--partition", choices=("all", *PARTITIONS), default="all")
    parser.add_argument("--output", type=Path, help="Write deterministic JSON snapshot")
    parser.add_argument("--measure", action="store_true",
                        help="Print optional host-only P50/P95/tracemalloc (non-golden)")
    parser.add_argument("--samples", type=int, default=5)
    args = parser.parse_args()
    result = run(args.partition)
    if args.measure:
        result["host_only_metrics"] = measure(args.samples)
    encoded = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
