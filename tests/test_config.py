"""Unit tests for config helpers and validation (alfred.config) following F.I.R.S.T. principles."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alfred.config import (
    _apply_env_overrides,
    _load_raw,
    _merge_defaults,
    _validate,
    load_config,
)


class ConfigHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_load_raw_defaults_when_path_is_none(self):
        raw = _load_raw(None)
        self.assertIn("audio", raw)
        self.assertIn("wake_word", raw)
        self.assertIn("qwen", raw)
        self.assertIn("runtime", raw)
        self.assertEqual(raw["wake_word"]["phrase"], "Alfred")

    def test_load_raw_merges_custom_file(self):
        cfg_file = Path(self.temp_dir.name) / "custom.json"
        cfg_file.write_text(json.dumps({"wake_word": {"phrase": "hey alfred"}}))
        raw = _load_raw(cfg_file)
        self.assertEqual(raw["wake_word"]["phrase"], "hey alfred")

    def test_load_raw_raises_on_unknown_key(self):
        cfg_file = Path(self.temp_dir.name) / "invalid.json"
        cfg_file.write_text(json.dumps({"unknown_section": {"foo": "bar"}}))
        with self.assertRaises(ValueError):
            _load_raw(cfg_file)

    def test_merge_defaults_returns_config(self):
        raw = _load_raw(None)
        merged = _merge_defaults(raw)
        self.assertEqual(merged, raw)

    def test_apply_env_overrides_gguf_and_model(self):
        raw = _load_raw(None)
        with patch.dict(
            os.environ,
            {
                "ALFRED_GGUF": "/tmp/custom.gguf",
                "ALFRED_QWEN_MODEL": "qwen-custom:4b",
                "ALFRED_QWEN_BACKEND": "ollama",
            },
        ):
            overridden = _apply_env_overrides(raw, None)
            self.assertEqual(overridden["qwen"]["model_path"], "/tmp/custom.gguf")
            self.assertEqual(overridden["qwen"]["model"], "qwen-custom:4b")
            self.assertEqual(overridden["qwen"]["backend"], "ollama")

    def test_apply_env_overrides_url(self):
        raw = _load_raw(None)
        with patch.dict(os.environ, {"ALFRED_QWEN_URL": "http://127.0.0.1:9999"}):
            overridden = _apply_env_overrides(raw, None)
            self.assertEqual(overridden["qwen"]["url"], "http://127.0.0.1:9999")

    def test_validate_passes_on_valid_config(self):
        raw = _load_raw(None)
        raw = _apply_env_overrides(raw, None)
        # Should not raise
        _validate(raw)

    def test_validate_rejects_invalid_backend(self):
        raw = _load_raw(None)
        raw["qwen"]["backend"] = "unsupported_backend"
        with self.assertRaises(ValueError):
            _validate(raw)

    def test_validate_rejects_non_loopback_url(self):
        raw = _load_raw(None)
        raw["qwen"]["url"] = "http://192.168.1.100:8080"
        with self.assertRaises(ValueError):
            _validate(raw)

    def test_validate_rejects_url_with_path(self):
        raw = _load_raw(None)
        raw["qwen"]["url"] = "http://127.0.0.1:8080/v1"
        with self.assertRaises(ValueError):
            _validate(raw)

    def test_validate_rejects_invalid_wake_phrase(self):
        raw = _load_raw(None)
        raw["wake_word"]["phrase"] = ""
        with self.assertRaises(ValueError):
            _validate(raw)

    def test_validate_rejects_invalid_wake_threshold(self):
        raw = _load_raw(None)
        raw["wake_word"]["threshold"] = 1.5
        with self.assertRaises(ValueError):
            _validate(raw)

    def test_load_config_integration(self):
        cfg = load_config()
        self.assertIn("qwen", cfg)
        self.assertIn("wake_word", cfg)


if __name__ == "__main__":
    unittest.main()
