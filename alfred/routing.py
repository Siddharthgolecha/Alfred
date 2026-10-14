"""Conservative explicit GPU-status intent guard for Needle 3 tool selection.

Needle may confuse a GPU query with overall system status. This module only
repairs *read-only status* selections; it never modifies mutating tool calls,
reasoner delegation, reminder creation, or ambiguous requests.
"""
from __future__ import annotations

import re

_GPU = re.compile(r"\b(?:gpu|graphics\s+(?:card|processor))\b", re.I)
_STATUS = re.compile(
    r"\b(?:status|temp(?:erature)?|usage|utili[sz]ation|load|health|"
    r"metric(?:s)?|memory|vram|frequency|clock|power)\b", re.I
)
# The presence of these words means the user may want both CPU/system and GPU
# status; do not drop an existing system-status call.
_OTHER_COMPONENT = re.compile(r"\b(?:cpu|system|overall|whole\s+device|ram|disk|storage)\b",re.I)
# Do not turn explanations, negated instructions or multi-action utterances
# into unconditional GPU reads.
_EXCLUSION = re.compile(
    r"\b(?:why|explain|compare|interpret|cause|reason|"
    r"don't|do\s+not|never|without|instead\s+of|"
    r"remind|reminder|schedule|timer|stop|start|restart|turn\s+off|delete)\b",
    re.I
)

def explicit_gpu_status_request(utterance: str) -> bool:
    """True only for straightforward spoken GPU status/metrics requests."""
    return bool(
        isinstance(utterance,str)
        and _GPU.search(utterance)
        and _STATUS.search(utterance)
        and not _EXCLUSION.search(utterance)
    )

def adjust_status_calls(utterance, calls):
    """Return a copied tool-call list; change only Needle status-only decisions."""
    if not explicit_gpu_status_request(utterance):
        return calls
    if not isinstance(calls,list) or any(
        not isinstance(call,dict) or call.get("name") not in
        ("get_gpu_status","get_system_status") for call in calls
    ):
        return calls
    if not calls:
        # A simple, unmistakably explicit status request does not require Qwen.
        return [{"name":"get_gpu_status","arguments":{}}]
    mixed=bool(_OTHER_COMPONENT.search(utterance))
    adjusted=[]
    for c in calls:
        if c["name"]=="get_system_status" and not mixed:
            adjusted.append({"name":"get_gpu_status","arguments":{}})
        else:
            adjusted.append(dict(c))
    if not any(c["name"]=="get_gpu_status" for c in adjusted):
        adjusted.append({"name":"get_gpu_status","arguments":{}})
    deduplicated=[]
    names=set()
    for item in adjusted:
        if item["name"] not in names:
            names.add(item["name"])
            deduplicated.append(item)
    return deduplicated
