"""Unit tests for alfred.validators — pure functions, no external deps."""
from __future__ import annotations

import json
import math
import unittest

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
from alfred.constants import TTL, Truncation


class TestValidateType(unittest.TestCase):
    def test_passes_correct_type(self):
        self.assertEqual(validate_type("hello", str), "hello")

    def test_passes_subclass(self):
        self.assertIsInstance(validate_type(True, int), bool)  # bool is subclass of int

    def test_raises_type_error_on_wrong_type(self):
        with self.assertRaises(TypeError):
            validate_type(42, str, "my_field")

    def test_error_message_contains_label(self):
        with self.assertRaises(TypeError) as ctx:
            validate_type(42, str, "username")
        self.assertIn("username", str(ctx.exception))


class TestValidateBool(unittest.TestCase):
    def test_accepts_true(self):
        self.assertTrue(validate_bool(True))

    def test_accepts_false(self):
        self.assertFalse(validate_bool(False))

    def test_rejects_int_one(self):
        with self.assertRaises(ValueError):
            validate_bool(1)

    def test_rejects_string(self):
        with self.assertRaises(ValueError):
            validate_bool("true")

    def test_error_message_contains_label(self):
        with self.assertRaises(ValueError) as ctx:
            validate_bool("yes", "enabled_flag")
        self.assertIn("enabled_flag", str(ctx.exception))


class TestValidateInt(unittest.TestCase):
    def test_accepts_int(self):
        self.assertEqual(validate_int(5), 5)

    def test_rejects_float(self):
        with self.assertRaises(ValueError):
            validate_int(5.0)

    def test_rejects_bool(self):
        # bool is a subclass of int but type(True) is bool, not int
        with self.assertRaises(ValueError):
            validate_int(True)

    def test_rejects_string(self):
        with self.assertRaises(ValueError):
            validate_int("5")


class TestValidateRange(unittest.TestCase):
    def test_accepts_within_range(self):
        self.assertEqual(validate_range(5, 1, 10), 5)

    def test_accepts_boundary_values(self):
        self.assertEqual(validate_range(1, 1, 10), 1)
        self.assertEqual(validate_range(10, 1, 10), 10)

    def test_rejects_below_min(self):
        with self.assertRaises(ValueError):
            validate_range(0, 1, 10)

    def test_rejects_above_max(self):
        with self.assertRaises(ValueError):
            validate_range(11, 1, 10)

    def test_error_message_contains_label(self):
        with self.assertRaises(ValueError) as ctx:
            validate_range(999, 1, 10, "timeout")
        self.assertIn("timeout", str(ctx.exception))


class TestValidateTTL(unittest.TestCase):
    def test_accepts_valid_context_ttl(self):
        self.assertEqual(validate_ttl_seconds(120), 120)

    def test_accepts_min_boundary(self):
        self.assertEqual(validate_ttl_seconds(TTL.CONTEXT_MIN), TTL.CONTEXT_MIN)

    def test_accepts_max_boundary(self):
        self.assertEqual(validate_ttl_seconds(TTL.CONTEXT_MAX), TTL.CONTEXT_MAX)

    def test_rejects_zero(self):
        with self.assertRaises(ValueError):
            validate_ttl_seconds(0)

    def test_rejects_too_large(self):
        with self.assertRaises(ValueError):
            validate_ttl_seconds(TTL.CONTEXT_MAX + 1)

    def test_rejects_non_int(self):
        with self.assertRaises(ValueError):
            validate_ttl_seconds(5.0)  # type: ignore[arg-type]

    def test_custom_range_approval(self):
        result = validate_ttl_seconds(
            TTL.APPROVAL_DEFAULT,
            min_val=TTL.APPROVAL_MIN,
            max_val=TTL.APPROVAL_MAX,
            label="approval_ttl",
        )
        self.assertEqual(result, TTL.APPROVAL_DEFAULT)

    def test_custom_range_rejects_below_min(self):
        with self.assertRaises(ValueError):
            validate_ttl_seconds(
                TTL.APPROVAL_MIN - 1,
                min_val=TTL.APPROVAL_MIN,
                max_val=TTL.APPROVAL_MAX,
            )


class TestValidateStringField(unittest.TestCase):
    def test_accepts_valid_string(self):
        self.assertEqual(validate_string_field("hello", 10), "hello")

    def test_accepts_exact_max_length(self):
        s = "a" * 10
        self.assertEqual(validate_string_field(s, 10), s)

    def test_rejects_too_long(self):
        with self.assertRaises(ValueError):
            validate_string_field("a" * 11, 10)

    def test_rejects_non_string(self):
        with self.assertRaises(ValueError):
            validate_string_field(123, 10)  # type: ignore[arg-type]

    def test_rejects_empty_by_default(self):
        with self.assertRaises(ValueError):
            validate_string_field("", 10)

    def test_accepts_empty_when_allowed(self):
        self.assertEqual(validate_string_field("", 10, allow_empty=True), "")


class TestTruncate(unittest.TestCase):
    def test_short_string_unchanged(self):
        self.assertEqual(truncate("hi", 100), "hi")

    def test_truncates_at_max_length(self):
        self.assertEqual(truncate("abcdef", 3), "abc")

    def test_default_length_is_message_constant(self):
        long_str = "x" * (Truncation.MESSAGE + 10)
        self.assertEqual(len(truncate(long_str)), Truncation.MESSAGE)

    def test_custom_length(self):
        self.assertEqual(truncate("hello world", 5), "hello")

    def test_empty_string(self):
        self.assertEqual(truncate("", 100), "")


class TestToCompactJson(unittest.TestCase):
    def test_basic_dict(self):
        result = to_compact_json({"b": 2, "a": 1})
        # Keys must be sorted
        self.assertEqual(result, '{"a":1,"b":2}')

    def test_no_spaces(self):
        result = to_compact_json({"x": [1, 2, 3]})
        self.assertNotIn(" ", result)

    def test_rejects_nan(self):
        with self.assertRaises(ValueError):
            to_compact_json({"v": float("nan")})

    def test_rejects_inf(self):
        with self.assertRaises(ValueError):
            to_compact_json({"v": float("inf")})

    def test_ensure_ascii(self):
        result = to_compact_json({"k": "café"})
        self.assertTrue(result.isascii())

    def test_roundtrip(self):
        data = {"a": 1, "b": [True, None, "hello"]}
        result = json.loads(to_compact_json(data))
        self.assertEqual(result, data)

    def test_with_default_callable(self):
        # Should not raise for a type that is not JSON-serialisable by default
        result = to_compact_json({"ts": object()}, default=str)
        self.assertIn('"ts"', result)

    def test_bytes_form(self):
        result = to_compact_json_bytes({"k": "v"})
        self.assertIsInstance(result, bytes)
        self.assertEqual(json.loads(result), {"k": "v"})


class TestFiniteFloat(unittest.TestCase):
    def test_accepts_int(self):
        self.assertEqual(finite_float(5), 5.0)

    def test_accepts_float(self):
        self.assertAlmostEqual(finite_float(3.14), 3.14)

    def test_rejects_nan(self):
        with self.assertRaises(ValueError):
            finite_float(float("nan"))

    def test_rejects_inf(self):
        with self.assertRaises(ValueError):
            finite_float(float("inf"))

    def test_rejects_string(self):
        with self.assertRaises(ValueError):
            finite_float("3.14")  # type: ignore[arg-type]


class TestClamp(unittest.TestCase):
    def test_within_range(self):
        self.assertEqual(clamp(5, 1, 10), 5)

    def test_below_min(self):
        self.assertEqual(clamp(-1, 1, 10), 1)

    def test_above_max(self):
        self.assertEqual(clamp(100, 1, 10), 10)

    def test_boundary(self):
        self.assertEqual(clamp(1, 1, 10), 1)
        self.assertEqual(clamp(10, 1, 10), 10)


if __name__ == "__main__":
    unittest.main()
