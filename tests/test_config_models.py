"""Tests for Pydantic v2 config models in alfred.config."""
from __future__ import annotations

import unittest

from pydantic import ValidationError

from alfred.config import (
    AlfredConfig,
    AudioConfig,
    QwenConfig,
    RuntimeConfig,
    WakeWordConfig,
    load_config,
    load_config_model,
)
from alfred.constants import Limits, TTL, Timeouts


class TestAudioConfig(unittest.TestCase):
    def test_defaults_valid(self):
        cfg = AudioConfig()
        self.assertEqual(cfg.mode, "auto")
        self.assertEqual(cfg.frame_ms, 500)

    def test_invalid_mode(self):
        with self.assertRaises(ValidationError):
            AudioConfig(mode="usb")

    def test_invalid_frame_ms(self):
        with self.assertRaises(ValidationError):
            AudioConfig(frame_ms=100)

    def test_valid_frame_ms_values(self):
        for ms in (250, 500, 1000):
            AudioConfig(frame_ms=ms)

    def test_probe_timeout_out_of_range(self):
        with self.assertRaises(ValidationError):
            AudioConfig(probe_timeout_seconds=0.5)

    def test_speech_rms_out_of_range(self):
        with self.assertRaises(ValidationError):
            AudioConfig(speech_rms=0.5)

    def test_extra_fields_forbidden(self):
        with self.assertRaises(ValidationError):
            AudioConfig(unknown_field="x")


class TestWakeWordConfig(unittest.TestCase):
    def test_defaults_valid(self):
        cfg = WakeWordConfig()
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.phrase, "Alfred")

    def test_invalid_mode(self):
        with self.assertRaises(ValidationError):
            WakeWordConfig(mode="sphinx")

    def test_phrase_must_match_pattern(self):
        with self.assertRaises(ValidationError):
            WakeWordConfig(phrase="123invalid")

    def test_phrase_must_not_have_trailing_space(self):
        with self.assertRaises(ValidationError):
            WakeWordConfig(phrase="Alfred ")

    def test_followup_out_of_range(self):
        with self.assertRaises(ValidationError):
            WakeWordConfig(followup_seconds=31)

    def test_threshold_out_of_range(self):
        with self.assertRaises(ValidationError):
            WakeWordConfig(threshold=0.0)


class TestQwenConfig(unittest.TestCase):
    def test_defaults_valid(self):
        cfg = QwenConfig()
        self.assertEqual(cfg.backend, "auto")
        self.assertTrue(cfg.enabled)

    def test_ctx_size_out_of_range(self):
        with self.assertRaises(ValidationError):
            QwenConfig(ctx_size=0)

    def test_supervisor_poll_out_of_range(self):
        with self.assertRaises(ValidationError):
            QwenConfig(supervisor_poll_seconds=5)


class TestRuntimeConfig(unittest.TestCase):
    def test_defaults_valid(self):
        cfg = RuntimeConfig()
        self.assertEqual(cfg.approval_ttl_seconds, TTL.APPROVAL_DEFAULT)
        self.assertEqual(cfg.max_plan_steps, Limits.MAX_PLAN_STEPS)

    def test_context_persistence_requires_context_enabled(self):
        with self.assertRaises(ValidationError):
            RuntimeConfig(context_persistence_enabled=True, context_enabled=False)

    def test_approval_ttl_too_low(self):
        with self.assertRaises(ValidationError):
            RuntimeConfig(approval_ttl_seconds=TTL.APPROVAL_MIN - 1)

    def test_approval_ttl_too_high(self):
        with self.assertRaises(ValidationError):
            RuntimeConfig(approval_ttl_seconds=TTL.APPROVAL_MAX + 1)

    def test_context_ttl_out_of_range(self):
        with self.assertRaises(ValidationError):
            RuntimeConfig(context_ttl_seconds=0)

    def test_harness_timeout_out_of_range(self):
        with self.assertRaises(ValidationError):
            RuntimeConfig(harness_timeout_seconds=Timeouts.HARNESS_TIMEOUT_MIN - 1)

    def test_max_plan_steps_out_of_range(self):
        with self.assertRaises(ValidationError):
            RuntimeConfig(max_plan_steps=0)

    def test_invalid_context_mode(self):
        with self.assertRaises(ValidationError):
            RuntimeConfig(context_mode="live")

    def test_invalid_context_owner(self):
        with self.assertRaises(ValidationError):
            RuntimeConfig(context_owner="1invalid!")

    def test_needle_max_decisions_out_of_range(self):
        with self.assertRaises(ValidationError):
            RuntimeConfig(needle_max_decisions=Limits.NEEDLE_MAX_DECISIONS + 1)


class TestAlfredConfig(unittest.TestCase):
    def test_defaults_valid(self):
        cfg = AlfredConfig()
        self.assertIsInstance(cfg.audio, AudioConfig)
        self.assertIsInstance(cfg.wake_word, WakeWordConfig)
        self.assertIsInstance(cfg.qwen, QwenConfig)
        self.assertIsInstance(cfg.runtime, RuntimeConfig)


class TestLoadConfigModel(unittest.TestCase):
    def test_returns_alfred_config(self):
        model = load_config_model()
        self.assertIsInstance(model, AlfredConfig)

    def test_matches_load_config(self):
        raw = load_config()
        model = load_config_model()
        self.assertEqual(model.runtime.approval_ttl_seconds,
                         raw["runtime"]["approval_ttl_seconds"])
        self.assertEqual(model.audio.mode, raw["audio"]["mode"])


if __name__ == "__main__":
    unittest.main()
