"""Offline, hardware-independent checks for Jetson SSD and meeting subsystems."""
from __future__ import annotations
from array import array
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
import wave

from alfred.jetson.storage import GuardedSSD,StorageSafetyError
from alfred.jetson.capture import Source,validate_sources,adjusted_seconds
from alfred.jetson.minutes import merge_channels,save_meeting_pdf,MinutesReasoner
from alfred.jetson.sharing import create_qr_handoff,send_bluetooth_opp
from alfred.jetson.cli import process_session,build_parser


def fake_mount(path):
    return {"target":str(path),"source":"/dev/sda1","fstype":"ext4","options":"rw"}


def example_minutes():
    return {"title":"Team sync","summary":"Discussed next steps.",
            "agenda":["Status updates"],"key_points":["A's findings","B's notes"],
            "decisions":["Plan approved"],"action_items":[{"task":"Send notes","owner":"Alice","due":"Tomorrow"}],
            "risks":["Limited memory"],"open_questions":["Next meeting date"]}


class GuardedStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.swap=self.root/"16GB.swap"
        self.swap.write_bytes(b"0")
        self.patches=[
            patch("pathlib.Path.is_mount",return_value=True),
            patch("alfred.jetson.storage._mount_at",side_effect=fake_mount),
            patch("alfred.jetson.storage._swaps",return_value=[
                {"path":str(self.swap),"size_bytes":16*1024**3,
                 "used_bytes":1024**3,"priority":-2}]),
        ]
        for p in self.patches:
            p.start()
        self.guard=GuardedSSD(self.root,self.swap,min_free_gib=0,min_ram_mib=0,
                             require_active_swap=True)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_ext4_and_swap_verified_without_modifying_kernel(self):
        report=self.guard.check()
        self.assertEqual(report.filesystem,"ext4")
        self.assertEqual(report.swap[0]["used_bytes"],1024**3)
        self.assertEqual(report.warnings,[])

    def test_safe_cache_paths_are_on_ssd(self):
        env=self.guard.cache_environment()
        self.assertTrue(env["HF_HUB_CACHE"].startswith(str(self.root)))
        self.assertTrue(env["HF_DATASETS_CACHE"].startswith(str(self.root)))
        self.assertTrue(env["TMPDIR"].startswith(str(self.root)))
        self.assertNotIn("HF_HOME",env)

    def test_unsafe_path_name_rejected(self):
        with self.assertRaises(StorageSafetyError):
            self.guard.directory("../escape")
        with self.assertRaises(StorageSafetyError):
            self.guard.path("meetings","../filename.pdf")

    def test_missing_mount_and_wrong_filesystem_fail_closed(self):
        with patch("pathlib.Path.is_mount",return_value=False):
            with self.assertRaisesRegex(StorageSafetyError,"not mounted"):
                self.guard.check()
        with patch("alfred.jetson.storage._mount_at",return_value={
                 "target":str(self.root),"source":"/dev/sda1","fstype":"exfat"}):
            with self.assertRaisesRegex(StorageSafetyError,"ext4"):
                self.guard.check()

    def test_device_replacement_fails(self):
        with patch("alfred.jetson.storage._mount_at",return_value={
            "target":str(self.root),"source":"/dev/sdb1","fstype":"ext4"}):
            with self.assertRaisesRegex(StorageSafetyError,"source changed"):
                self.guard.check()

    def test_missing_active_swap_configurable(self):
        with patch("alfred.jetson.storage._swaps",return_value=[]):
            with self.assertRaisesRegex(StorageSafetyError,"not active"):
                self.guard.check()
        self.guard.require_active_swap=False
        with patch("alfred.jetson.storage._swaps",return_value=[]):
            self.assertTrue(self.guard.check().warnings)

    def test_json_write_stays_on_ssd(self):
        result=self.guard.write_json("meetings","report.json",{"hello":"world"})
        self.assertEqual(json.loads(result.read_text()),{"hello":"world"})
        self.assertEqual(result.parent,self.root/"alfred"/"meetings")


class MultimicTests(unittest.TestCase):
    def test_distinct_mics_and_speakers(self):
        a=Source("Alice","alsa_input.usb_one")
        b=Source("Bob","bluez_input.XX")
        items=[{"name":a.pulse_source,"monitor_of_sink":None},
               {"name":b.pulse_source,"monitor_of_sink":None}]
        with patch("alfred.jetson.capture.pactl_json",return_value=items):
            self.assertTrue(validate_sources([a,b]))
        with self.assertRaises(ValueError):
            validate_sources([a,a])
        with self.assertRaises(ValueError):
            Source("bad name","alsa_input.usb_1")

    def test_calibration_offsets_are_measured_not_assumed(self):
        source=Source("Alice","mic",offset_ms=50,drift_ppm=100)
        shifted=adjusted_seconds(1700000000,16000,source)
        self.assertAlmostEqual(shifted,1700000001.0501,places=3)

    def test_merge_orders_overlapping_speaker_windows(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            entries={}
            for name,delay in (("Alice",0),("Bob",.25)):
                path=root/(name+".wav")
                with wave.open(str(path),"wb") as f:
                    f.setframerate(16000)
                    f.setsampwidth(2)
                    f.setnchannels(1)
                    f.writeframes(array("h",[5000]*16000).tobytes())
                entries[name]={"source":"mic"+name,"wav":str(path),
                               "first_sample_unix_estimate":1700000000+delay}
            class Whistle:
                def stream(self,frames,**kwargs):
                    for _ in frames: pass
                    yield {"text":"hello","pending":""}
            segments=merge_channels(entries,Whistle())
            self.assertEqual([s["speaker"] for s in segments],["Alice","Bob"])
            self.assertAlmostEqual(segments[1]["start_seconds"]-segments[0]["start_seconds"],.25,places=2)
            self.assertEqual(segments[0]["precision"],"window-level, not word-level")


class MeetingOutputTests(GuardedStorageTests):
    def test_pdf_then_qr_with_integrity_checksum(self):
        folder=self.guard.directory("meetings")/"20261014-test"
        folder.mkdir()
        segments=[{"speaker":"Alice","start_utc":"2026-10-14T10:00:00Z",
                   "end_utc":"2026-10-14T10:00:10Z","text":"Planning <review>."}]
        pdf=save_meeting_pdf(self.guard,folder,example_minutes(),segments)
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
        qr,manifest=create_qr_handoff(pdf,folder,adapter="00:11:22:33:44:55")
        self.assertTrue(qr.is_file())
        self.assertEqual(manifest["sha256"],hashlib.sha256(pdf.read_bytes()).hexdigest())
        self.assertNotIn("Planning",json.dumps(manifest))
        self.assertIn("QR is handoff metadata",manifest["note"])

    def test_pdf_path_escape_blocked(self):
        outside=self.root.parent/("outside-"+self.root.name)
        with self.assertRaises(StorageSafetyError):
            save_meeting_pdf(self.guard,outside,example_minutes(),[])

    def test_invalid_pdf_tool_fields_rejected(self):
        with self.assertRaises(ValueError):
            save_meeting_pdf(self.guard,self.guard.directory("meetings"),
                             {"title":"unsafe"},[])

    def test_qwen_requires_save_meeting_pdf_tool_call(self):
        cfg={"url":"http://127.0.0.1:8080","model_path":"",
             "expected_model":"Qwen3.5-4B"}
        reasoner=MinutesReasoner(cfg)
        segments=[{"speaker":"Alice","start_utc":"2026-10-14T10:00:00Z",
                   "end_utc":"2026-10-14T10:00:10Z","text":"Approved plan.",
                   "start_seconds":1,"end_seconds":11}]
        def response(system,user,schema,max_tokens=1200):
            if "notes" in schema["properties"]:
                return {"notes":"Alice approved the plan."}
            return {"tool_calls":[{"name":"save_meeting_pdf","arguments":example_minutes()}]}
        with patch.object(reasoner,"_complete",side_effect=response):
            result=reasoner.summarize(segments)
        self.assertEqual(result["decisions"],["Plan approved"])
        with patch.object(reasoner,"_complete",return_value={
                "tool_calls":[{"name":"run_shell","arguments":{}}]}):
            with self.assertRaises(ValueError):
                reasoner.summarize(segments)

    def test_bluetooth_send_reuses_existing_pdf_and_verifies_hash(self):
        from alfred.jetson.cli import main
        folder=self.guard.directory("meetings")/"send-session"
        folder.mkdir()
        (folder/"capture_manifest.json").write_text(json.dumps({"channels":{}}))
        pdf=folder/"minutes.pdf"
        pdf.write_bytes(b"%PDF simulated minutes")
        qr,manifest=create_qr_handoff(pdf,folder,adapter="00:11:22:33:44:55")
        argv=["--ssd",str(self.root),"send","--session",str(folder),
              "--recipient","11:22:33:44:55:66"]
        with patch("alfred.jetson.cli.GuardedSSD",return_value=self.guard),\
             patch("alfred.jetson.cli.send_bluetooth_opp",return_value={
                "transport":"bluetooth_opp"}) as send:
            self.assertEqual(main(argv),0)
            send.assert_called_once_with(pdf.resolve(),"11:22:33:44:55:66")
            # Tampering with the PDF invalidates the QR manifest checksum.
            pdf.write_bytes(b"%PDF tampered")
            self.assertEqual(main(argv),2)
            self.assertEqual(send.call_count,1)

    def test_end_to_end_resume_with_mock_whistle_reasoner(self):
        folder=self.guard.directory("meetings")/"meeting-one"
        folder.mkdir()
        audio=folder/"Alice.wav"
        with wave.open(str(audio),"wb") as f:
            f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000)
            f.writeframes(array("h",[5000]*16000).tobytes())
        info={"channels":{"Alice":{"source":"alsa_input.usb_mic","wav":str(audio),
                  "first_sample_unix_estimate":1700000000}}}
        (folder/"capture_manifest.json").write_text(json.dumps(info))
        class Whistle:
            def stream(self,frames,**kwargs):
                for _ in frames:pass
                yield {"text":"Discussed plan"}
        reasoner=Mock()
        reasoner.summarize.return_value=example_minutes()
        conf={"qwen":{"url":"http://127.0.0.1:8080"}}
        with patch("alfred.jetson.sharing.adapter_address",return_value=None):
            result=process_session(self.guard,folder,conf,"Team sync",
                                   whistle=Whistle(),reasoner=reasoner)
        self.assertTrue(Path(result["pdf"]).is_file())
        self.assertTrue(Path(result["qr"]).is_file())
        self.assertEqual(result["transfer"],"not started; QR alone does not send a Bluetooth file")


class BluetoothTransferTests(unittest.TestCase):
    def test_requires_prepaired_trusted_recipient(self):
        with tempfile.TemporaryDirectory() as folder:
            pdf=Path(folder)/"minutes.pdf"
            pdf.write_bytes(b"%PDF test")
            with patch("alfred.jetson.sharing.shutil.which",side_effect=lambda name:name),\
                 patch("alfred.jetson.sharing.subprocess.run",
                       return_value=Mock(returncode=0,stdout="Paired: no\nTrusted: no",stderr="")):
                with self.assertRaises(PermissionError):
                    send_bluetooth_opp(pdf,"00:11:22:33:44:55")
            with self.assertRaises(ValueError):
                send_bluetooth_opp(pdf,"not-a-mac")

    def test_opp_transfer_uses_argv_no_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            pdf=Path(folder)/"minutes.pdf"
            pdf.write_bytes(b"%PDF test")
            output=Mock(returncode=0,stdout="Paired: yes\nTrusted: yes\n",stderr="")
            with patch("alfred.jetson.sharing.shutil.which",side_effect=lambda name:name),\
                 patch("alfred.jetson.sharing.subprocess.run",
                       return_value=output) as run:
                result=send_bluetooth_opp(pdf,"00:11:22:33:44:55")
            self.assertEqual(result["transport"],"bluetooth_opp")
            self.assertEqual(run.call_args_list[-1].args[0][0],"bt-obex")
            self.assertEqual(run.call_args_list[-1].args[0][1],"-p")


if __name__=="__main__":
    unittest.main()
