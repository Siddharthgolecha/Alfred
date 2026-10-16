"""Regression checks for the optional ONNX-only wakeword dependency graph.

These offline tests run on Python 3.10+ and don't import optional native libraries.
"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WakeWordPackagingTests(unittest.TestCase):
    def test_uv_excludes_unused_tflite_runtime(self):
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertIn('exclude-dependencies = ["tflite-runtime"]', pyproject)

    def test_checked_in_lock_matches_onnx_only_exclusion(self):
        lock = (ROOT / "uv.lock").read_text()
        self.assertIn('[manifest]\nexcludes = ["tflite-runtime"]', lock)
        self.assertIn('name = "openwakeword"', lock)
        self.assertIn('name = "onnxruntime"', lock)
        self.assertNotIn('name = "tflite-runtime"', lock)

    def test_acoustic_detector_explicitly_selects_onnx(self):
        source = (ROOT / "alfred" / "wakeword.py").read_text()
        self.assertIn('inference_framework="onnx"', source)


if __name__ == "__main__":
    unittest.main()
