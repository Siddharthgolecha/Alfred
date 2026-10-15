"""Shared, reusable validation and serialisation helpers for Alfred.

All repeated validation patterns — TTL range checks, type assertions, string
truncation, and compact JSON serialisation — live here so they are tested once
and imported everywhere rather than re-implemented inline.
"""
from __future__ import annotations

import json
import math
from typing import Any, TypeVar

from .constants import TTL, Truncation

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Type checking
# ---------------------------------------------------------------------------

def validate_type(value: Any, expected: type[T], label: str = "value") -> T:
    """Raise :exc:`TypeError` if *value* is not an instance of *expected*.

    Uses ``isinstance`` at runtime so instances of subclasses pass.
    The ``TypeVar("T")`` parameter is for static type annotations,
    preserving the expected type on return. For strict identity checks
    (e.g. ``type(x) is int``) use the explicit ``type()`` form at the
    call site — this helper is for the common class-hierarchy case.
    """
    if not isinstance(value, expected):
        raise TypeError(
            f"{label} must be {expected.__name__}, got {type(value).__name__}"
        )
    return value  # type: ignore[return-value]


def validate_bool(value: Any, label: str = "value") -> bool:
    """Raise :exc:`ValueError` if *value* is not a ``bool`` (exact type check)."""
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def validate_int(value: Any, label: str = "value") -> int:
    """Raise :exc:`ValueError` if *value* is not an ``int`` (exact type check)."""
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    return value


# ---------------------------------------------------------------------------
# Range validation
# ---------------------------------------------------------------------------

def validate_range(value: int | float, lo: int | float, hi: int | float,
                   label: str = "value") -> int | float:
    """Return *value* if ``lo <= value <= hi``, otherwise raise :exc:`ValueError`."""
    if not lo <= value <= hi:
        raise ValueError(f"{label} must be {lo}..{hi}, got {value!r}")
    return value


def validate_ttl_seconds(value: Any, *,
                         min_val: int = TTL.CONTEXT_MIN,
                         max_val: int = TTL.CONTEXT_MAX,
                         label: str = "ttl_seconds") -> int:
    """Validate that *value* is an int in [*min_val*, *max_val*] (inclusive).

    Defaults to the context TTL range (1..300). Pass ``min_val``/``max_val``
    to override for approval or needle slot TTLs.
    """
    validate_int(value, label)
    validate_range(value, min_val, max_val, label)
    return value  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# String helpers
# ---------------------------------------------------------------------------

def validate_string_field(value: Any, max_length: int, label: str = "field",
                           *, allow_empty: bool = False) -> str:
    """Validate that *value* is a non-empty string within *max_length* chars.

    Args:
        value:       The candidate value to validate.
        max_length:  Maximum allowed character length (inclusive).
        label:       Field name used in error messages.
        allow_empty: If ``True``, an empty string passes length validation.

    Returns:
        The validated string, unchanged.
    """
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if not allow_empty and not value:
        raise ValueError(f"{label} must not be empty")
    if len(value) > max_length:
        raise ValueError(f"{label} exceeds maximum length of {max_length}")
    return value


def truncate(text: str, max_length: int = Truncation.MESSAGE) -> str:
    """Return *text* truncated to at most *max_length* characters.

    Uses a named constant from :mod:`alfred.constants.Truncation` as the
    default so call sites can write ``truncate(msg)`` without repeating the
    magic number.
    """
    return text[:max_length]


# ---------------------------------------------------------------------------
# JSON serialisation
# ---------------------------------------------------------------------------

def to_compact_json(data: Any, *, default: Any = None) -> str:
    """Serialise *data* to a compact, sorted, ASCII-safe JSON string.

    Equivalent to::

        json.dumps(data, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=True, allow_nan=False, default=default)

    This is the canonical serialisation form used throughout Alfred.
    """
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=default,
    )


def to_compact_json_bytes(data: Any, *, default: Any = None) -> bytes:
    """Return the UTF-8 encoded form of :func:`to_compact_json`."""
    return to_compact_json(data, default=default).encode("utf-8")


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------

def finite_float(value: Any, label: str = "value") -> float:
    """Return *value* as a float if it is finite; raise :exc:`ValueError` otherwise."""
    if type(value) not in (int, float):
        raise ValueError(f"{label} must be a number")
    f = float(value)
    if not math.isfinite(f):
        raise ValueError(f"{label} must be finite, got {value!r}")
    return f


def clamp(value: int | float, lo: int | float, hi: int | float) -> int | float:
    """Return *value* clamped to [*lo*, *hi*]."""
    return max(lo, min(hi, value))
