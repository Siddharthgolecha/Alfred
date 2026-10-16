"""System inspection, GPU telemetry, systemd services, and safe task execution tools."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from ..constants import Limits, Patterns, Timeouts, Truncation
from ..platform_utils import sample_resources as _platform_sample_resources, system_uptime_seconds
from .base import default_registry, num_schema, str_schema


def meminfo() -> dict[str, int]:
    """Return available memory in MiB; cross-platform."""
    sample = _platform_sample_resources()
    out: dict[str, int] = {}
    if sample.get("available_mib") is not None:
        out["MemAvailable"] = int(sample["available_mib"])
    return out


def gpu_status() -> dict[str, Any]:
    """Take a bounded tegrastats sample without leaving a background process."""
    binary = shutil.which("tegrastats")
    if not binary:
        return {"available": False, "reason": "tegrastats not installed"}
    p = subprocess.Popen(
        [binary, "--interval", "1000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        import select

        ready, _, _ = select.select([p.stdout], [], [], Timeouts.TEGRASTATS_READ)
        line = p.stdout.readline().strip() if ready else ""
    finally:
        p.terminate()
        try:
            p.wait(timeout=Timeouts.TEGRASTATS_TERMINATE)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=Timeouts.TEGRASTATS_TERMINATE)
        if p.stdout:
            p.stdout.close()
    temps = {k: float(v) for k, v in re.findall(r"([A-Za-z_]+)@([0-9.]+)C", line)}
    return {
        "available": bool(line),
        "temperatures_c": temps,
        "gpu_temp_c": temps.get("GPU"),
        "raw": line[: Truncation.GPU_RAW],
    }


def system_status() -> dict[str, Any]:
    """Report memory, CPU load, disk space and system uptime."""
    disk = shutil.disk_usage("/")
    try:
        load = os.getloadavg()
    except (OSError, AttributeError):
        load = (None, None, None)
    return {
        "load_1_5_15": load,
        "memory_mib": meminfo(),
        "root_free_gib": round(disk.free / 1024**3, 2),
        "uptime_seconds": system_uptime_seconds(),
    }


def _valid_name(name: str) -> bool:
    """Return True if name is a safe service or task name."""
    return isinstance(name, str) and bool(Patterns.VALID_NAME.fullmatch(name))


@default_registry.tool(
    name="get_system_status",
    description="Report memory, CPU load, disk and uptime on Jetson",
)
def handle_get_system_status(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    from .. import runtime as rt

    return rt.status()


@default_registry.tool(
    name="get_gpu_status",
    description="Report Jetson GPU status, temperature and tegrastats sample",
)
def handle_get_gpu_status(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    from .. import runtime as rt

    return rt.gpu_status()


@default_registry.tool(
    name="list_services",
    description="List status of approved systemd user services",
)
def handle_list_services(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    return {
        service_name: context._service(service_name, "is-active")
        for service_name in context.services
    }


@default_registry.tool(
    name="control_service",
    description="Start or stop a preapproved user service",
    properties={
        "name": str_schema("Exact allowed service name"),
        "action": {"type": "string", "enum": ["start", "stop"]},
    },
    required=["name", "action"],
)
def handle_control_service(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    service_name = args["name"]
    action = args["action"]
    if service_name not in context.services or not _valid_name(service_name):
        raise PermissionError("Service not in runtime.allowed_services")
    return {
        "name": service_name,
        "action": action,
        "result": context._service(service_name, action),
    }


@default_registry.tool(
    name="run_named_task",
    description="Run a preconfigured safe script by task name",
    properties={"name": str_schema("Exact approved named task")},
    required=["name"],
)
def handle_run_named_task(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    task_name = args["name"]
    spec = context.tasks.get(task_name)
    if not isinstance(spec, list) or not spec or any(not isinstance(arg, str) for arg in spec):
        raise PermissionError("Task not in runtime.named_tasks")
    runner = context.processes.run if context.processes else subprocess.run
    completed = runner(spec, capture_output=True, text=True, timeout=60, check=False)
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout[-Truncation.RESULT :],
        "stderr": completed.stderr[-Truncation.STDERR :],
    }
