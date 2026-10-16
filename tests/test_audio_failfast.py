"""Regression tests for Jetson phantom Built-in Audio and missing microphone failure."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from alfred.audio import AudioManager, AudioUnavailableError, source_status
from alfred.app import doctor, main
from alfred.config import load_config
from alfred.platform_utils import IS_LINUX

BUILTIN = {
    "name":"alsa_input.platform-tegra-sound.stereo-fallback",
    "properties":{"device.description":"Built-in Audio"},
    "monitor_of_sink":4294967295,
    "ports":[{"name":"analog-input-internal-mic","available":"unknown"}]
}
USB = {
    "name":"alsa_input.usb-Logitech_Headset.analog-mono",
    "properties":{"device.description":"USB headset microphone","device.bus":"usb"},
    "monitor_of_sink":4294967295,
    "ports":[{"name":"analog-input-mic","available":"yes"}]
}
MONITOR = {"name":"alsa_output.platform.stereo.monitor",
           "properties":{"device.description":"Built-in Audio Monitor"},
           "monitor_of_sink":1}
BT = {"name":"bluez_input.AA_BB_CC_DD_EE_FF.0",
      "properties":{"device.description":"Sony WI-C100"},
      "monitor_of_sink":4294967295}

def fake_sources(items):
    def probe(target):
        if target=="cards":
            return []
        if target=="sources":
            return items
        raise ValueError(target)
    return probe

@unittest.skipUnless(IS_LINUX, "Linux PulseAudio/PipeWire tests (skipped on non-Linux)")
class SourceClassificationTests(unittest.TestCase):
    def setUp(self):
        self.cfg=load_config()["audio"]
        self.manager=AudioManager(self.cfg)

    def test_jetson_built_in_audio_without_available_mic_rejected(self):
        ok,reason=source_status(BUILTIN)
        self.assertFalse(ok)
        self.assertIn("built-in",reason)
        with patch("alfred.audio.pactl_json",side_effect=fake_sources([BUILTIN])):
            self.assertIsNone(self.manager.choose_source())
            entries=self.manager.diagnostics()
            self.assertFalse(entries[0]["eligible"])
            self.assertIn("built-in",entries[0]["reason"])

    def test_no_input_port_declared_rejected_for_built_in(self):
        entry=dict(BUILTIN, ports=[])
        self.assertFalse(source_status(entry)[0])

    def test_built_in_with_positive_port_not_automatically_trusted(self):
        entry=dict(BUILTIN,ports=[{"name":"analog-input-mic","available":"yes"}])
        ok,reason=source_status(entry)
        self.assertFalse(ok)
        self.assertIn("auto-trusted",reason)
        self.assertTrue(source_status(entry,explicitly_selected=True)[0])

    def test_explicit_direct_source_still_needs_probe(self):
        self.cfg["mode"]="direct"
        self.cfg["direct_source"]=BUILTIN["name"]
        with patch("alfred.audio.pactl_json",side_effect=fake_sources([BUILTIN])):
            self.assertEqual(self.manager.choose_source(),BUILTIN["name"])
            with patch.object(self.manager,"_open") as op, \
                 patch.object(self.manager,"_read_pcm",
                              side_effect=AudioUnavailableError("No frames")):
                with self.assertRaisesRegex(AudioUnavailableError,"No usable microphone"):
                    self.manager.ensure_input()
                op.assert_called_once()

    def test_unavailable_port_rejected_even_if_explicit(self):
        entry=dict(USB,ports=[{"name":"analog-input-mic","available":"no"}])
        self.assertFalse(source_status(entry,explicitly_selected=True)[0])

    def test_virtual_monitors_rejected(self):
        self.assertFalse(source_status(MONITOR)[0])
        self.assertFalse(source_status({"name":"auto_null.monitor"})[0])

    def test_usb_and_sony_mic_eligible_before_capture_probe(self):
        self.assertTrue(source_status(USB)[0])
        self.assertTrue(source_status(BT)[0])

    def test_no_usable_input_raises_instead_of_retrying(self):
        with patch("alfred.audio.pactl_json",side_effect=fake_sources([BUILTIN,MONITOR])):
            with self.assertRaisesRegex(AudioUnavailableError,"No usable microphone detected"):
                self.manager.ensure_input()
            with self.assertRaises(AudioUnavailableError):
                next(self.manager.frames())

    def test_usb_source_selected_over_phantom_built_in(self):
        with patch("alfred.audio.pactl_json",side_effect=fake_sources([BUILTIN,USB])):
            self.assertEqual(self.manager.choose_source(),USB["name"])

    def test_probe_can_fallback_to_second_eligible_source(self):
        another=dict(USB,name="alsa_input.usb-Backup.analog-mono")
        with patch("alfred.audio.pactl_json",side_effect=fake_sources([USB,another])), \
             patch.object(self.manager,"_open") as opener, \
             patch.object(self.manager,"_read_pcm",
                  side_effect=[AudioUnavailableError("unresponsive"),b"\x00"*3200]):
            chosen=self.manager.ensure_input()
        self.assertIn(chosen,(USB["name"],another["name"]))
        self.assertEqual(opener.call_count,2)

    def test_repeated_pcm_failure_exits_instead_of_endless_restart(self):
        self.manager.process=object()
        self.manager.last_check=__import__("time").monotonic()
        self.cfg["poll_seconds"]=10000
        def recover():
            self.manager.process=object()
        def close():
            self.manager.process=None
            self.manager._prefetched=b""
        with patch.object(self.manager,"_read_pcm",
                          side_effect=AudioUnavailableError("stream timeout")), \
             patch.object(self.manager,"ensure_input",side_effect=recover) as retry, \
             patch.object(self.manager,"close",side_effect=close):
            with self.assertRaisesRegex(AudioUnavailableError,"repeatedly stopped"):
                next(self.manager.frames())
        self.assertEqual(retry.call_count,2)

    def test_audio_server_down_reports_failure(self):
        with patch("alfred.audio.pactl_json",side_effect=OSError("socket unavailable")):
            with self.assertRaisesRegex(AudioUnavailableError,"Cannot enumerate"):
                self.manager.ensure_input()


class CLIFailFastTests(unittest.TestCase):
    def test_voice_startup_exits_nonzero_before_cactus_loaded(self):
        with patch("alfred.cli.AudioManager.ensure_input",
                   side_effect=AudioUnavailableError("No headset connected")), \
             patch("alfred.cli.Alfred") as model_start:
            result=main(["--config","/does/not/exist.json"])
        self.assertEqual(result,2)
        model_start.assert_not_called()

    def test_text_mode_does_not_require_mic(self):
        with patch("alfred.cli.Alfred") as app, \
             patch("alfred.cli.AudioManager.ensure_input") as check:
            result=main(["--text","GPU status","--config","/does/not/exist.json"])
        self.assertEqual(result,0)
        check.assert_not_called()
        app.return_value.handle_text.assert_called_once_with("GPU status")

    def test_doctor_reports_failed_detection_and_exits_nonzero(self):
        with patch("alfred.cli.AudioManager.diagnostics",return_value=[
             {"name":BUILTIN["name"],"description":"Built-in Audio",
              "eligible":False,"reason":"no confirmed input port"}]), \
             patch("alfred.cli.AudioManager.ensure_input",
                   side_effect=AudioUnavailableError("no mic")), \
             patch("alfred.cli.pactl_json",return_value=[]), \
             patch("alfred.cli.LlamaSupervisor") as supervisor, \
             patch("builtins.print") as output:
            supervisor.return_value.status.return_value={"health":"down"}
            result=main(["--doctor","--config","/does/not/exist.json"])
        self.assertEqual(result,2)
        text=" ".join(str(c.args) for c in output.call_args_list)
        self.assertIn("REJECTED",text)
        self.assertIn("MICROPHONE NOT CONNECTED",text)

    def test_doctor_success_requires_actual_capture(self):
        with patch("alfred.cli.AudioManager.diagnostics",return_value=[]), \
             patch("alfred.cli.AudioManager.ensure_input",return_value=USB["name"]), \
             patch("alfred.cli.pactl_json",return_value=[]), \
             patch("alfred.cli.LlamaSupervisor") as supervisor, \
             patch("alfred.cli.LocalHarness") as harness:
            supervisor.return_value.status.return_value={"health":"ready","model":"Qwen3.5-4B"}
            harness.return_value.audit_installation.return_value={
                "sdk_version":"0.1.5rc1","runtime_version":"0.1.5rc1","profile":"sdk-minimal"}
            result=main(["--doctor","--config","/does/not/exist.json"])
        self.assertEqual(result,0)

    def test_doctor_rejects_sdk_startup_failure_despite_valid_plugin_audit(self):
        from alfred.cognitive.harness_sdk import HarnessUnavailable
        with patch("alfred.cli.AudioManager.diagnostics",return_value=[]), \
             patch("alfred.cli.AudioManager.ensure_input",return_value=USB["name"]), \
             patch("alfred.cli.pactl_json",return_value=[]), \
             patch("alfred.cli.LlamaSupervisor") as supervisor, \
             patch("alfred.cli.LocalHarness") as harness:
            supervisor.return_value.status.return_value={
                "health":"ready","model":"Qwen3.5-4B"}
            harness.return_value.audit_installation.return_value={
                "sdk_version":"0.1.5rc1","runtime_version":"0.1.5rc1"}
            harness.return_value.require_ready.side_effect=HarnessUnavailable(
                "Cordis SDK child failed to boot")
            self.assertEqual(main(["--doctor","--config","/does/not/exist.json"]),2)

    def test_doctor_rejects_cordis_under_low_pressure_without_spawn(self):
        from alfred.inference_admission import AdmissionRejected
        from alfred.config import load_config
        cfg=load_config()
        cfg["runtime"]["resource_admission_enabled"]=True
        with patch("alfred.cli.load_config",return_value=cfg), \
             patch("alfred.cli.AudioManager.diagnostics",return_value=[]), \
             patch("alfred.cli.AudioManager.ensure_input",return_value=USB["name"]), \
             patch("alfred.cli.pactl_json",return_value=[]), \
             patch("alfred.cli.LlamaSupervisor") as supervisor, \
             patch("alfred.cli.LocalHarness") as harness, \
             patch("alfred.cli.InferenceAdmission") as gate, \
             patch("builtins.print") as printed:
            supervisor.return_value.status.return_value={
                "health":"ready","model":"Qwen3.5-4B"}
            harness.return_value.audit_installation.side_effect=AdmissionRejected(
                "Inference refused due to resource pressure")
            result=main(["--doctor","--config","/does/not/exist.json"])
            self.assertEqual(result,2)
            self.assertTrue(harness.call_args.kwargs["admission"] is gate.return_value)
            harness.return_value.require_ready.assert_not_called()
            text=" ".join(str(c.args) for c in printed.call_args_list)
            self.assertIn("resource pressure",text)

if __name__=="__main__":
    unittest.main()
