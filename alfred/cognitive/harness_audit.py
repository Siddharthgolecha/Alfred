"""Cordis plugin-graph audit helpers for the DeepSeek Harness SDK.

These functions verify the exact, pinned plugin composition of the
sdk-minimal Cordis overlay before any SDK process is started.  They are
intentionally separate from the SDK client (harness_sdk.py) so they can be
unit-tested independently and imported without instantiating a LocalHarness.
"""
from __future__ import annotations

from pathlib import Path

from ..constants import Patterns, Truncation
from .harness_config import DENIED_ROWS, EXPECTED_ACTIVE_ROWS, EXPECTED_DISABLED_ROWS

# Default patch path — same directory as this module.
_DEFAULT_PATCH = Path(__file__).with_name("alfred_safe.patch.yml")


class HarnessUnavailable(RuntimeError):
    """The SDK subprocess or its required plugin graph is not available."""


def audit_effective_cordis_config(dump: str) -> dict[str, list[str]]:
    """Fail closed on unexpected plugin permissions in official --dump-config.

    The official CLI emits an ordered YAML list of top-level plugin rows,
    with source comments and possible !!js config expressions. Parse only
    *literal* top-level id/name/disabled metadata; never evaluate JS tags,
    YAML constructors, or external plugin commands.
    """
    if type(dump) is not str or not 0 < len(dump) < Truncation.CORDIS_DUMP_BYTES:
        raise HarnessUnavailable("Invalid Cordis config dump length")
    rows: dict[str, dict] = {}
    current: dict | None = None
    for line in dump.splitlines():
        if line.startswith("- id: "):
            name = line[6:]
            if (not Patterns.CORDIS_ROW_ID.fullmatch(name)
                    or name in rows):
                raise HarnessUnavailable("Duplicate or invalid Cordis row")
            rows[name] = {"name": None, "disabled": False}
            current = rows[name]
        elif line.startswith("- ") and not line.startswith("- id: "):
            raise HarnessUnavailable("Unknown top-level Cordis entry")
        elif current is not None and line.startswith("  name: "):
            if current["name"] is not None:
                raise HarnessUnavailable("Duplicate plugin name")
            current["name"] = line[8:].strip("'\\\"")
        elif current is not None and line.startswith("  disabled: "):
            if line != "  disabled: true":
                raise HarnessUnavailable("Dynamic or enabled plugin state")
            current["disabled"] = True
    expected = set(EXPECTED_ACTIVE_ROWS) | set(EXPECTED_DISABLED_ROWS)
    if set(rows) != expected:
        raise HarnessUnavailable("Unexpected effective Cordis plugin inventory")
    for key, name in EXPECTED_ACTIVE_ROWS.items():
        if rows[key] != {"name": name, "disabled": False}:
            raise HarnessUnavailable("Unexpected active Cordis plugin")
    for key, name in EXPECTED_DISABLED_ROWS.items():
        if rows[key] != {"name": name, "disabled": True}:
            raise HarnessUnavailable("Unsafe Cordis plugin is enabled")
    return {"active": sorted(EXPECTED_ACTIVE_ROWS),
            "disabled": sorted(EXPECTED_DISABLED_ROWS)}


def check_safe_patch(path: "Path | str" = _DEFAULT_PATCH) -> str:
    """Validate the version-specific Cordis overlay before any SDK launch.

    Rejects new/unexpected rows, JS expressions, duplicates, plugins, enabling
    rows or modifying the SDK server/model provider. Uses exact text grammar
    rather than a full yaml parser (to avoid implicit custom-tag execution).

    Returns the SHA-256 hex digest of the raw patch bytes.
    """
    import hashlib

    raw = Path(path).read_bytes()
    if len(raw) > Truncation.PATCH_BYTES:
        raise HarnessUnavailable("Unsafe Cordis overlay size")
    lines = []
    for rawline in raw.decode("utf-8").splitlines():
        line = rawline.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    if len(lines) != 2 * len(DENIED_ROWS):
        raise HarnessUnavailable("Cordis disable list incomplete")
    seen: set[str] = set()
    for i in range(0, len(lines), 2):
        a, b = lines[i:i+2]
        if not Patterns.CORDIS_PATCH_LINE.fullmatch(a) or b != "disabled: true":
            raise HarnessUnavailable("Cordis overlay contains unauthorized syntax")
        row = a[6:]
        if row not in DENIED_ROWS or row in seen:
            raise HarnessUnavailable("Unexpected or duplicated Cordis row")
        seen.add(row)
    if seen != DENIED_ROWS:
        raise HarnessUnavailable("Unsafe Cordis overlay missing rows")
    return hashlib.sha256(raw).hexdigest()
