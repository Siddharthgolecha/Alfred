"""Explicit release policy: offline CI can NEVER qualify native Jetson rollout."""
from __future__ import annotations

import math


class ReleaseBlocked(RuntimeError):
    pass


REQUIRED_METRICS=("p50_latency_ms","p95_latency_ms","qwen_cold_load_ms",
                  "qwen_warm_load_ms","peak_ram_mib","peak_swap_used_mib",
                  "peak_cpu_load_per_core","peak_gpu_utilization_pct")


def evaluate_release(report, thresholds, *, acknowledge_hardware=False):
    """Verdict with machine-readable reasons; no model/OS side effects.

    Release requires an independently collected 8 GB Orin report with all
    actual measured metrics, agreed thresholds, 100+ test cases, known
    no-replay/authorization errors, recorded provenance and owner approval.
    No defaults or placeholder data are accepted.
    """
    reasons=[]
    if type(report) is not dict or type(thresholds) is not dict:
        raise ValueError("Release report and thresholds must be JSON objects")
    if report.get("native_jetson_tested") is not True:
        reasons.append("No physical Jetson measurements")
    if report.get("device") != "NVIDIA Jetson Orin Nano 8GB":
        reasons.append("Device identity not confirmed")
    if (type(report.get("fixture_count")) is not int
            or report["fixture_count"]<100):
        reasons.append("Insufficient case coverage")
    if not acknowledge_hardware:
        reasons.append("Local hardware owner has not approved the release")
    if not isinstance(report.get("sources"),list) or not report["sources"]:
        reasons.append("Missing test run provenance")
    gate=report.get("release_gates")
    if type(gate) is not dict:
        gate={}
    for name in ("unauthorized_tool_executions","unintended_replay"):
        if type(gate.get(name)) is not int or gate[name]!=0:
            reasons.append("Safety gate failed or unmeasured: "+name)
    if gate.get("plugin_escape_attempts_succeeded") != 0:
        reasons.append("Plugin sandbox/egress negative cases not clean")
    for metric in REQUIRED_METRICS:
        measured=report.get(metric)
        agreed=thresholds.get("max_"+metric)
        if (type(measured) not in (int,float) or not math.isfinite(measured)
                or measured<0):
            reasons.append("Missing or invalid measured "+metric)
            continue
        if (type(agreed) not in (int,float) or not math.isfinite(agreed)
                or agreed<=0):
            reasons.append("No agreed threshold for "+metric)
        elif measured>agreed:
            reasons.append("Threshold exceeded for "+metric)
    if report.get("models_real") is not True or report.get("offline") is not True:
        reasons.append("Native offline model execution not verified")
    if report.get("voice_capture_deadline_passed") is not True:
        reasons.append("Whistle capture deadline not verified")
    if report.get("scheduler_deadline_passed") is not True:
        reasons.append("Reminders/scheduler deadline not verified")
    if report.get("clean_owned_shutdown") is not True:
        reasons.append("Owned subprocess shutdown not verified")
    if report.get("dependencies_merged") is not True:
        reasons.append("Prerequisite issues #13–#17 not integrated in main")
    # Unknown flags are never permission to overlook missing measurements.
    return {"release_allowed":not reasons,"reasons":reasons,
            "checked_metrics":len(REQUIRED_METRICS),
            "policy":"explicit_hardware_owner_opt_in_only"}


def require_release(report,thresholds,*,acknowledge_hardware=False):
    verdict=evaluate_release(report,thresholds,
                             acknowledge_hardware=acknowledge_hardware)
    if not verdict["release_allowed"]:
        raise ReleaseBlocked("; ".join(verdict["reasons"]))
    return verdict
