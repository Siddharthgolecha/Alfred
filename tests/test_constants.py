"""Smoke tests: all constants are importable and have sensible values."""
from __future__ import annotations

import re

import pytest

from alfred.constants import Limits, Patterns, Timeouts, Truncation, TTL


class TestTruncation:
    def test_all_attrs_positive_ints(self):
        for name in vars(Truncation):
            if not name.startswith("_"):
                val = getattr(Truncation, name)
                assert isinstance(val, int) and val > 0, f"Truncation.{name}={val!r}"

    def test_contract_bytes_under_64k(self):
        assert Truncation.CONTRACT_BYTES <= 65536


class TestLimits:
    def test_max_facts_positive(self):
        assert Limits.MAX_FACTS > 0

    def test_context_budget_min_less_than_max(self):
        assert Limits.CONTEXT_BUDGET_CHARS_MIN < Limits.CONTEXT_BUDGET_CHARS_MAX

    def test_gpu_temp_min_less_than_max(self):
        assert Limits.GPU_TEMP_MIN_C < Limits.GPU_TEMP_MAX_C


class TestTimeouts:
    def test_harness_min_less_than_max(self):
        assert Timeouts.HARNESS_TIMEOUT_MIN < Timeouts.HARNESS_TIMEOUT_MAX

    def test_all_positive(self):
        for name in vars(Timeouts):
            if not name.startswith("_"):
                val = getattr(Timeouts, name)
                assert isinstance(val, (int, float)) and val > 0, f"Timeouts.{name}={val!r}"


class TestTTL:
    def test_context_min_less_than_max(self):
        assert TTL.CONTEXT_MIN < TTL.CONTEXT_MAX

    def test_approval_min_less_than_max(self):
        assert TTL.APPROVAL_MIN < TTL.APPROVAL_MAX


class TestPatterns:
    def test_all_attrs_are_compiled_patterns(self):
        for name in vars(Patterns):
            if not name.startswith("_"):
                val = getattr(Patterns, name)
                assert isinstance(val, re.Pattern), f"Patterns.{name} is not compiled"

    def test_correlation_id_matches_32_hex(self):
        assert Patterns.CORRELATION_ID.fullmatch("a" * 32)
        assert not Patterns.CORRELATION_ID.fullmatch("x" * 32)
        assert not Patterns.CORRELATION_ID.fullmatch("a" * 31)

    def test_tool_name_pattern(self):
        assert Patterns.TOOL_NAME.fullmatch("get_gpu_status")
        assert not Patterns.TOOL_NAME.fullmatch("1bad")
