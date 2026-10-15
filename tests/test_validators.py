"""Unit tests for alfred/validators.py helpers."""
from __future__ import annotations

import json
import math
import pytest

from alfred.validators import (
    clamp,
    finite_float,
    to_compact_json,
    to_compact_json_bytes,
    truncate,
    validate_bool,
    validate_int,
    validate_range,
    validate_string_field,
    validate_ttl_seconds,
    validate_type,
)
from alfred.constants import Truncation, TTL


# ---------------------------------------------------------------------------
# validate_type
# ---------------------------------------------------------------------------

class TestValidateType:
    def test_passes_for_correct_type(self):
        assert validate_type(42, int) == 42

    def test_passes_for_subclass(self):
        class Sub(int): pass
        assert validate_type(Sub(5), int) == 5

    def test_raises_for_wrong_type(self):
        with pytest.raises(TypeError, match="int"):
            validate_type("hello", int)

    def test_custom_label_in_error(self):
        with pytest.raises(TypeError, match="myfield"):
            validate_type(1.0, int, "myfield")


# ---------------------------------------------------------------------------
# validate_bool / validate_int
# ---------------------------------------------------------------------------

class TestValidateBool:
    def test_true_and_false(self):
        assert validate_bool(True) is True
        assert validate_bool(False) is False

    def test_int_rejected(self):
        with pytest.raises(ValueError):
            validate_bool(1)

    def test_none_rejected(self):
        with pytest.raises(ValueError):
            validate_bool(None)


class TestValidateInt:
    def test_positive_int(self):
        assert validate_int(7) == 7

    def test_zero(self):
        assert validate_int(0) == 0

    def test_float_rejected(self):
        with pytest.raises(ValueError):
            validate_int(1.0)

    def test_bool_rejected(self):
        with pytest.raises(ValueError):
            validate_int(True)


# ---------------------------------------------------------------------------
# validate_range
# ---------------------------------------------------------------------------

class TestValidateRange:
    def test_in_range(self):
        assert validate_range(5, 1, 10) == 5

    def test_at_bounds(self):
        assert validate_range(1, 1, 10) == 1
        assert validate_range(10, 1, 10) == 10

    def test_out_of_range(self):
        with pytest.raises(ValueError):
            validate_range(0, 1, 10)
        with pytest.raises(ValueError):
            validate_range(11, 1, 10)


# ---------------------------------------------------------------------------
# validate_ttl_seconds
# ---------------------------------------------------------------------------

class TestValidateTtlSeconds:
    def test_default_range(self):
        assert validate_ttl_seconds(TTL.CONTEXT_MIN) == TTL.CONTEXT_MIN
        assert validate_ttl_seconds(TTL.CONTEXT_MAX) == TTL.CONTEXT_MAX

    def test_out_of_default_range_rejected(self):
        with pytest.raises(ValueError):
            validate_ttl_seconds(0)
        with pytest.raises(ValueError):
            validate_ttl_seconds(TTL.CONTEXT_MAX + 1)

    def test_custom_range(self):
        assert validate_ttl_seconds(
            TTL.APPROVAL_MIN, min_val=TTL.APPROVAL_MIN, max_val=TTL.APPROVAL_MAX
        ) == TTL.APPROVAL_MIN

    def test_float_rejected(self):
        with pytest.raises(ValueError):
            validate_ttl_seconds(30.0)

    def test_bool_rejected(self):
        with pytest.raises(ValueError):
            validate_ttl_seconds(True)


# ---------------------------------------------------------------------------
# validate_string_field
# ---------------------------------------------------------------------------

class TestValidateStringField:
    def test_valid(self):
        assert validate_string_field("hello", 10) == "hello"

    def test_at_max_length(self):
        assert validate_string_field("a" * 10, 10) == "a" * 10

    def test_over_max_length(self):
        with pytest.raises(ValueError, match="exceeds"):
            validate_string_field("a" * 11, 10)

    def test_empty_rejected_by_default(self):
        with pytest.raises(ValueError, match="must not be empty"):
            validate_string_field("", 10)

    def test_empty_allowed_when_flag_set(self):
        assert validate_string_field("", 10, allow_empty=True) == ""

    def test_non_string_rejected(self):
        with pytest.raises(ValueError):
            validate_string_field(42, 10)


# ---------------------------------------------------------------------------
# truncate
# ---------------------------------------------------------------------------

class TestTruncate:
    def test_no_truncation_needed(self):
        assert truncate("hi", 10) == "hi"

    def test_truncates_to_limit(self):
        assert truncate("abcdef", 3) == "abc"

    def test_default_uses_message_constant(self):
        long = "x" * (Truncation.MESSAGE + 50)
        result = truncate(long)
        assert len(result) == Truncation.MESSAGE

    def test_zero_limit(self):
        assert truncate("abc", 0) == ""


# ---------------------------------------------------------------------------
# to_compact_json
# ---------------------------------------------------------------------------

class TestToCompactJson:
    def test_produces_sorted_compact_json(self):
        result = to_compact_json({"b": 2, "a": 1})
        assert result == '{"a":1,"b":2}'

    def test_no_nan_allowed(self):
        with pytest.raises(ValueError):
            to_compact_json({"v": float("nan")})

    def test_no_inf_allowed(self):
        with pytest.raises(ValueError):
            to_compact_json(float("inf"))

    def test_nested_structure(self):
        data = {"x": [1, 2], "y": {"z": True}}
        parsed = json.loads(to_compact_json(data))
        assert parsed == data

    def test_empty_dict(self):
        assert to_compact_json({}) == "{}"

    def test_ascii_safe(self):
        result = to_compact_json({"k": "café"})
        assert all(ord(c) < 128 for c in result)


class TestToCompactJsonBytes:
    def test_returns_bytes(self):
        result = to_compact_json_bytes({"a": 1})
        assert isinstance(result, bytes)
        assert result == b'{"a":1}'


# ---------------------------------------------------------------------------
# finite_float
# ---------------------------------------------------------------------------

class TestFiniteFloat:
    def test_int_coerced(self):
        assert finite_float(3) == 3.0

    def test_finite_float(self):
        assert finite_float(1.5) == 1.5

    def test_nan_rejected(self):
        with pytest.raises(ValueError, match="finite"):
            finite_float(float("nan"))

    def test_inf_rejected(self):
        with pytest.raises(ValueError):
            finite_float(math.inf)

    def test_string_rejected(self):
        with pytest.raises(ValueError):
            finite_float("1.0")


# ---------------------------------------------------------------------------
# clamp
# ---------------------------------------------------------------------------

class TestClamp:
    def test_within_range(self):
        assert clamp(5, 1, 10) == 5

    def test_below_lo(self):
        assert clamp(-1, 0, 10) == 0

    def test_above_hi(self):
        assert clamp(20, 0, 10) == 10

    def test_at_bounds(self):
        assert clamp(0, 0, 10) == 0
        assert clamp(10, 0, 10) == 10
