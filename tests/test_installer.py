"""Offline tests for Alfred's standard-library installer and Windows audio path."""
import io
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.installer import core
from scripts.installer.ui import Terminal
from alfred.audio import AudioManager, WindowsAudioManager, _windows_capture_command
from alfred.feedback import _tts_argv
from alfred.platform_utils import IS_WINDOWS


class InstallerUITests(unittest.TestCase):
    def test_numbered_prompt_uses_default_and_stable_value(self):
        out = io.StringIO()
        ui = Terminal(stream=out, input_stream=io.StringIO("\n"), plain=True)
        value = ui.choose("Choose", [("a", "First", "help"), ("b", "Second", "")], default=1)
        self.assertEqual(value, "b")
        self.assertIn("1. First", out.getvalue())
        self.assertIn("2. Second (recommended)", out.getvalue())

    def test_eof_is_actionable_instead_of_looping_forever(self):
        ui = Terminal(stream=io.StringIO(), input_stream=io.StringIO(), plain=True)
        with self.assertRaisesRegex(RuntimeError, "Input ended"):
            ui.choose("Choose", [("a", "First", "")])

    def test_platform_runtime_map(self):
        self.assertEqual(core.platform_key("Linux", "aarch64"), "linux_aarch64")
        self.assertEqual(core.platform_key("Darwin", "arm64"), "macos_aarch64")
        self.assertEqual(core.platform_key("Windows", "AMD64"), "windows_x86_64")
        with self.assertRaisesRegex(RuntimeError, "No pinned"):
            core.platform_key("Windows", "arm64")


class ConfigPersistenceTests(unittest.TestCase):
    def test_config_is_atomic_preserves_unrelated_keys_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "folder with spaces" / "config.json"
            path.parent.mkdir()
            path.write_text('{"other":{"keep":true},"qwen":{"backend":"auto"}}\n')
            config = json.loads(path.read_text())
            config["qwen"]["backend"] = "ollama"
            core.save_config(path, config)
            self.assertEqual(json.loads(path.read_text())["other"], {"keep": True})
            self.assertEqual(json.loads(path.read_text())["qwen"]["backend"], "ollama")
            self.assertTrue(path.with_suffix(".json.bak").exists())
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_wheelhouse_is_strict_and_rejects_wrong_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdk_bytes = b"pinned sdk test bytes"
            runtime_bytes = b"pinned runtime test bytes"
            lock = {"version": core.RUNTIME, "sdk": {"filename": "sdk.whl", "sha256": hashlib.sha256(sdk_bytes).hexdigest()},
                    "runtime": {"linux_x86_64": {"filename": "runtime.whl", "sha256": hashlib.sha256(runtime_bytes).hexdigest()}}}
            root = Path(tmp)
            (root / "coordination").mkdir()
            (root / "coordination" / "deepseek-harness-lock.json").write_text(json.dumps(lock))
            (root / "wheels").mkdir()
            (root / "wheels" / "sdk.whl").write_bytes(sdk_bytes)
            (root / "wheels" / "runtime.whl").write_bytes(runtime_bytes)
            with patch.object(core, "ROOT", root):
                self.assertEqual(len(core._wheel_records(root / "wheels", "linux_x86_64")), 2)
                (root / "wheels" / "runtime.whl").write_bytes(b"tampered")
                with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                    core._wheel_records(root / "wheels", "linux_x86_64")
                (root / "wheels" / "runtime.whl").unlink()
                with self.assertRaisesRegex(RuntimeError, "missing"):
                    core._wheel_records(root / "wheels", "linux_x86_64")

    def test_explicit_backend_overrides_saved_and_environment_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "coordination").mkdir()
            (root / "config.example.json").write_text(json.dumps({
                "audio": {}, "qwen": {"backend": "ollama", "model": ""}}))
            args = SimpleNamespace(backend="lmstudio", auto=False, customize=False,
                                   skip_models=True, service=False, wheelhouse=None)
            ui = Terminal(stream=io.StringIO(), input_stream=io.StringIO(), plain=True)
            with patch.object(core, "ROOT", root), patch.object(core, "_detect_audio", return_value=[]), \
                 patch.object(core, "_detect_backend", return_value=[]), \
                 patch.dict(os.environ, {"ALFRED_BACKEND": "llamacpp"}):
                wizard = core.Wizard(args, ui)
                self.assertEqual(wizard.choose_backend(), "lmstudio")

    def test_auto_requires_a_backend_when_config_is_still_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "coordination").mkdir()
            (root / "config.example.json").write_text(json.dumps({
                "audio": {}, "qwen": {"backend": "auto", "model": ""}}))
            args = SimpleNamespace(backend=None, auto=True, customize=False,
                                   skip_models=False, service=False, wheelhouse=None)
            ui = Terminal(stream=io.StringIO(), input_stream=io.StringIO(), plain=True)
            with patch.object(core, "ROOT", root), patch.object(core, "_detect_audio", return_value=[]), \
                 patch.object(core, "_detect_backend", return_value=[]), \
                 patch.dict(os.environ, {"ALFRED_BACKEND": ""}, clear=False):
                wizard = core.Wizard(args, ui)
                with self.assertRaisesRegex(RuntimeError, "--auto needs a configured backend"):
                    wizard.choose_backend()

    def test_windows_tts_passes_encoded_text_as_data(self):
        payload = "special ' ; $(text)"
        with patch("alfred.feedback.IS_WINDOWS", True):
            argv = _tts_argv(payload)
        self.assertEqual(argv[:3], ["powershell", "-NoProfile", "-EncodedCommand"])
        self.assertNotIn(payload, argv[-1])


class WindowsAudioTests(unittest.TestCase):
    def test_directshow_capture_command_uses_raw_mono_pcm(self):
        argv = _windows_capture_command('USB Microphone "Studio"')
        self.assertIn('audio="USB Microphone \\"Studio\\""', argv)
        self.assertEqual(argv[-3:], ["-f", "s16le", "-"])
        self.assertEqual(argv[argv.index("-ac") + 1], "1")

    @patch("alfred.audio.subprocess.run")
    @patch("alfred.audio.shutil.which", return_value="ffmpeg")
    def test_directshow_device_enumeration_and_audio_manager(self, _which, run):
        run.return_value = Mock(stderr=(
            '[dshow @ 0] "USB Microphone" (audio)\n'
            '[dshow @ 0] "USB Microphone" (audio)\n'
            '[dshow @ 0] "Integrated Mic" (audio)\n'), returncode=1)
        from alfred.audio import _windows_audio_devices
        self.assertEqual(_windows_audio_devices(), ["USB Microphone", "Integrated Mic"])
        with patch("alfred.audio.IS_WINDOWS", True), patch("alfred.audio.IS_MACOS", False):
            manager = AudioManager({"mode": "auto", "direct_source": ""})
        self.assertIsInstance(manager._windows, WindowsAudioManager)
        self.assertEqual(manager.choose_source(), "USB Microphone")

    @patch("alfred.audio.subprocess.run")
    @patch("alfred.audio.shutil.which", return_value=None)
    def test_missing_ffmpeg_is_a_precise_diagnostic(self, _which, run):
        manager = WindowsAudioManager({"direct_source": ""})
        row = manager.diagnostics()[0]
        self.assertFalse(row["eligible"])
        self.assertIn("FFmpeg", row["reason"])
        with self.assertRaisesRegex(Exception, "FFmpeg"):
            manager.ensure_input()
        run.assert_not_called()

    @unittest.skipUnless(IS_WINDOWS, "Windows resource API requires a Windows runner")
    def test_windows_resource_gate_has_measured_fields(self):
        from alfred.platform_utils import sample_resources
        measured = sample_resources()
        self.assertGreaterEqual(measured["available_mib"], 0)
        self.assertGreaterEqual(measured["swap_used_mib"], 0)
        self.assertGreaterEqual(measured["cpu_load_per_core"], 0)


if __name__ == "__main__":
    unittest.main()
