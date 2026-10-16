"""Offline tests for Alfred's standard-library installer and Windows audio path."""
import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import urllib.error
import unittest
from unittest.mock import Mock, patch

from scripts.installer import core
from scripts.installer.state import InstallerLock, InstallerState
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

    def test_arrow_navigation_and_back_keep_selection_stable(self):
        ui = Terminal(stream=io.StringIO(), input_stream=io.StringIO(), plain=True)
        ui.arrows = True
        with patch.object(ui, "_key", side_effect=["down", "enter"]):
            value = ui.choose("Choose", [("a", "First", ""), ("b", "Second", "")])
        self.assertEqual(value, "b")
        with patch.object(ui, "_key", return_value="b"):
            self.assertEqual(ui.choose("Choose", [("a", "First", "")], back=True), "__back__")

    def test_plain_back_and_redirected_output_have_no_terminal_controls(self):
        out = io.StringIO()
        ui = Terminal(stream=out, input_stream=io.StringIO("b\n"), plain=True)
        self.assertEqual(ui.choose("Choose", [("a", "First", "")], back=True), "__back__")
        self.assertNotIn("\033", out.getvalue())

    def test_platform_runtime_map(self):
        self.assertEqual(core.platform_key("Linux", "aarch64"), "linux_aarch64")
        self.assertEqual(core.platform_key("Darwin", "arm64"), "macos_aarch64")
        self.assertEqual(core.platform_key("Windows", "AMD64"), "windows_x86_64")
        with self.assertRaisesRegex(RuntimeError, "No pinned"):
            core.platform_key("Windows", "arm64")


class ConfigPersistenceTests(unittest.TestCase):
    def test_project_verification_reads_installed_metadata_outside_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "checkout"
            root.mkdir()
            expected = {"version": "0.1.0", "direct": json.dumps({
                "url": root.resolve().as_uri(), "dir_info": {"editable": True}})}
            result = subprocess.CompletedProcess([], 0, stdout=json.dumps(expected), stderr="")
            with patch.object(core, "ROOT", root), \
                 patch.object(core.subprocess, "run", return_value=result) as run_probe:
                self.assertTrue(core.Wizard.__new__(core.Wizard)._project_ready(Path("python")))
            self.assertEqual(run_probe.call_args.kwargs["cwd"], root.parent)

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

    def test_quick_setup_preserves_custom_backend_url_and_missing_bluetooth_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "coordination").mkdir()
            (root / "config.example.json").write_text(json.dumps({
                "audio": {"mode": "bluetooth", "bluetooth_address": "AA:BB:CC:DD:EE:FF",
                          "direct_source": "bluez_input.AA_BB_CC_DD_EE_FF.0"},
                "qwen": {"backend": "ollama", "url": "http://127.0.0.1:22434",
                         "model": "custom:latest"}}))
            args = SimpleNamespace(backend=None, auto=False, customize=False,
                                   skip_models=True, service=False, wheelhouse=None)
            ui = Terminal(stream=io.StringIO(), input_stream=io.StringIO(), plain=True)
            with patch.object(core, "ROOT", root), patch.object(core, "_detect_audio", return_value=[]), \
                 patch.object(core, "_detect_backend", return_value=[]):
                wizard = core.Wizard(args, ui)
                wizard.quick = True
                wizard.choose_audio()
                wizard.settings.update(backend="ollama", model="custom:latest")
                self.assertEqual(wizard.settings["audio"], "saved")
                wizard._configure()
                self.assertEqual(wizard.config["audio"]["bluetooth_address"], "AA:BB:CC:DD:EE:FF")
                self.assertEqual(wizard.config["audio"]["direct_source"],
                                 "bluez_input.AA_BB_CC_DD_EE_FF.0")
                self.assertEqual(wizard.config["qwen"]["url"], "http://127.0.0.1:22434")

    def test_installer_state_is_atomic_and_scoped_to_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_home = root / "state"
            with patch("scripts.installer.state.state_root", return_value=state_home):
                state = InstallerState(root / "checkout")
                inputs = {"python": "3.12", "lock": "abc"}
                state.complete("runtime", inputs, output={"pydantic": "2.12.0"})
                state.remember_choices({"backend": "ollama"}, base_config_hash="base")
                loaded = InstallerState(root / "checkout")
                self.assertTrue(loaded.matches("runtime", inputs))
                self.assertEqual(loaded.data["steps"]["runtime"]["output"],
                                 {"pydantic": "2.12.0"})
                self.assertEqual(loaded.saved_choices()["values"], {"backend": "ollama"})
                self.assertFalse(loaded.matches("runtime", {**inputs, "python": "3.10"}))
                self.assertNotEqual(loaded.path, InstallerState(root / "other-checkout").path)
                self.assertEqual(list(loaded.path.parent.glob("*.tmp")), [])

    def test_checkout_lock_rejects_concurrent_installers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("scripts.installer.state.state_root", return_value=root / "state"):
                with InstallerLock(root / "checkout"):
                    with self.assertRaisesRegex(RuntimeError, "Another Alfred installer"):
                        with InstallerLock(root / "checkout"):
                            pass

    def test_unchanged_second_install_reuses_environment_packages_and_harness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "coordination").mkdir()
            (root / "pyproject.toml").write_text("[project]\nname='alfred-jetson'\nversion='0.1.0'\n")
            (root / "uv.lock").write_text("locked packages")
            sdk_data, runtime_data = b"verified sdk wheel", b"verified runtime wheel"
            sdk_sha, runtime_sha = (hashlib.sha256(sdk_data).hexdigest(),
                                    hashlib.sha256(runtime_data).hexdigest())
            lock = {"version": core.RUNTIME,
                    "sdk": {"filename": "sdk.whl", "sha256": sdk_sha},
                    "runtime": {"linux_x86_64": {"filename": "runtime.whl",
                                                    "sha256": runtime_sha}}}
            (root / "coordination" / "deepseek-harness-lock.json").write_text(json.dumps(lock))
            wheels = [root / "sdk.whl", root / "runtime.whl"]
            wheels[0].write_bytes(sdk_data)
            wheels[1].write_bytes(runtime_data)
            state_dir = root / "state"
            packages = {"cactus-needle": "3.2.2", "pydantic": "2.14.1",
                        "alfred-jetson": "0.1.0"}
            ui = SimpleNamespace(ok=Mock(), info=Mock(), warn=Mock(), section=Mock(),
                                 out=io.StringIO())
            args = SimpleNamespace(auto=True, repair=False, wheelhouse=None)
            with patch.object(core, "ROOT", root), \
                 patch.object(core, "state_root", return_value=state_dir), \
                 patch("scripts.installer.state.state_root", return_value=state_dir), \
                 patch.object(core, "_install_uv", return_value="/tools/uv"), \
                 patch.object(core, "_ensure_system_tools"), \
                 patch.object(core, "_detect_backend", return_value=[]), \
                 patch.object(core, "_detect_audio", return_value=[]), \
                 patch.object(core, "_package_snapshot", side_effect=lambda *_args: dict(packages)), \
                 patch.object(core, "_download_verified_wheels", return_value=wheels) as download, \
                 patch.object(core, "_call") as call, \
                 patch.object(core.Wizard, "_environment_ready", return_value=True), \
                 patch.object(core.Wizard, "_project_ready", return_value=True), \
                 patch("pathlib.Path.home", return_value=root / "home"), \
                 patch.object(core.Wizard, "_install_launcher") as launcher:
                wizard = core.Wizard.__new__(core.Wizard)
                wizard.args, wizard.ui, wizard.system = args, ui, "Linux"
                wizard.config_path = root / "config.json"
                wizard.config = {"audio": {"mode": "auto", "direct_source": ""},
                                 "qwen": {"backend": "auto", "url": "", "model": ""}}
                wizard.original_config = json.loads(json.dumps(wizard.config))
                wizard.installed = False
                wizard.settings = {"runtime_target": "linux_x86_64", "backend": "skip",
                                   "model": "", "audio": "saved", "skip_models": True,
                                   "service": False, "auto": True}
                wizard.state = InstallerState(root)
                wizard.existing_backends, wizard.existing_audio = [], []
                def install_command(argv, _ui, label, **_kwargs):
                    if label == "Installing verified DeepSeek Harness":
                        packages.update({"deepseek-harness-sdk": core.RUNTIME,
                                         "deepseek-harness-runtime-bin": core.RUNTIME})
                    return subprocess.CompletedProcess(argv, 0)
                call.side_effect = install_command
                def write_launcher(_python):
                    path = Path.home() / ".local" / "bin" / "alfred"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("#!/bin/sh\nexec " + str(root / ".venv" / "bin" / "python") +
                                    " -m alfred --config " + str(root / "config.json") +
                                    ' "$@"\n', encoding="utf-8")
                    path.chmod(0o755)
                launcher.side_effect = write_launcher
                wizard.install()
                first_run_commands = call.call_count
                first_run_downloads = download.call_count
                wizard.install()
                self.assertGreater(first_run_commands, 0)
                self.assertEqual(call.call_count, first_run_commands)
                self.assertEqual(download.call_count, first_run_downloads)
                self.assertEqual(first_run_downloads, 1)
                self.assertEqual(launcher.call_count, 1)

    def test_harness_download_resumes_partial_wheel_and_reuses_verified_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "coordination").mkdir()
            sdk, runtime = b"pinned sdk bytes", b"pinned runtime bytes for range resume"
            lock = {"version": core.RUNTIME,
                    "sdk": {"filename": "sdk.whl", "sha256": hashlib.sha256(sdk).hexdigest(),
                            "url": "https://example.invalid/sdk.whl"},
                    "runtime": {"linux_x86_64": {
                        "filename": "runtime.whl", "sha256": hashlib.sha256(runtime).hexdigest(),
                        "url": "https://example.invalid/runtime.whl"}}}
            (root / "coordination" / "deepseek-harness-lock.json").write_text(json.dumps(lock))
            cache = root / "cache"
            cache.mkdir()
            (cache / "sdk.whl").write_bytes(sdk)
            partial = cache / "runtime.whl.part"
            partial.write_bytes(runtime[:5])

            class Response(io.BytesIO):
                def __init__(self, data, start, *, fail_after=None):
                    super().__init__(data)
                    self.status = 206
                    self.headers = {"Content-Range": f"bytes {start}-{start + len(data) - 1}/{len(runtime)}",
                                    "Content-Length": str(len(data))}
                    self.fail_after = fail_after
                    self.read_count = 0
                def getcode(self):
                    return self.status
                def read(self, size=-1):
                    self.read_count += 1
                    if self.fail_after and self.read_count > self.fail_after:
                        raise urllib.error.URLError("connection reset")
                    return super().read(size)

            from urllib.request import Request
            seen_ranges = []
            def open_range(request, timeout):
                offset = int(request.get_header("Range").split("=")[1].split("-")[0])
                seen_ranges.append(offset)
                if len(seen_ranges) == 1:
                    return Response(runtime[offset:offset + 8], offset, fail_after=1)
                return Response(runtime[offset:], offset)

            ui = SimpleNamespace(ok=Mock(), info=Mock(), warn=Mock(),
                                 download_progress=Mock(), finish_download=Mock())
            with patch.object(core, "ROOT", root), \
                 patch("urllib.request.urlopen", side_effect=open_range), \
                 patch.object(core.time, "sleep"):
                wheels = core._download_verified_wheels(cache, "linux_x86_64", ui)
            self.assertEqual(seen_ranges, [5, 13])
            self.assertEqual(wheels[0].read_bytes(), sdk)
            self.assertEqual(wheels[1].read_bytes(), runtime)
            self.assertFalse(partial.exists())
            self.assertTrue(any("verified" in call.args[0].lower()
                                for call in ui.ok.call_args_list))

    def test_systemd_service_quotes_paths_and_includes_selected_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            service_root = home / "Alfred project"
            config = service_root / "config file.json"
            ui = SimpleNamespace(ok=Mock(), warn=Mock())
            calls = []
            def systemctl(argv, **_kwargs):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0)
            with patch.object(core.platform, "system", return_value="Linux"), \
                 patch.object(core, "_has", return_value=True), \
                 patch("scripts.installer.core.Path.home", return_value=home), \
                 patch.object(core.subprocess, "run", side_effect=systemctl):
                self.assertTrue(core._service(ui, Path("/venvs/my python"), service_root, config))
            unit = (home / ".config" / "systemd" / "user" / "alfred.service").read_text()
            self.assertIn('WorkingDirectory="' + str(service_root) + '"', unit)
            self.assertIn('ExecStart="/venvs/my python" "-m" "alfred" "--config" "' +
                          str(config) + '"', unit)
            self.assertEqual(len(calls), 3)

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
