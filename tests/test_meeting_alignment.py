"""Hardware-independent regression tests for automatic N-mic meeting ASR."""
from __future__ import annotations

from array import array
from pathlib import Path
from unittest.mock import patch,Mock
import io
import json
import math
import tempfile
import unittest
import wave

from alfred.jetson.capture import Source
from alfred.jetson.discovery import discover_mics,MAX_MICS
from alfred.jetson.calibration import calibrate_claps,adjusted_time
from alfred.jetson.transcription import merge_channels,annotate_overlap,pretty_transcript
from alfred.jetson.console import MeetingDisplay
from alfred.jetson.cli import build_parser


def wavfile(path,signal,rate=16000):
    with wave.open(str(path),"wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(array("h",signal).tobytes())


class SourceDiscoveryTests(unittest.TestCase):
    def test_automatic_usb_hub_and_bluetooth_count(self):
        inputs=[
            {"name":"alsa_input.usb-MicB","properties":{"device.bus":"usb"},"monitor_of_sink":None},
            {"name":"alsa_input.usb-MicA","properties":{"device.bus":"usb"},"monitor_of_sink":None},
            {"name":"bluez_input.AA_BB_CC","monitor_of_sink":None},
            {"name":"alsa_input.platform_tegra","properties":{"device.description":"Built-in Audio"}},
            {"name":"alsa_output.usb-MicA.monitor","monitor_of_sink":1}
        ]
        with patch("alfred.jetson.discovery.probe_microphone",return_value=True):
            chosen=discover_mics(pulse_sources=inputs)
        self.assertEqual(len(chosen),3)
        self.assertEqual([c.speaker for c in chosen],
                         ["USB_1","USB_2","Bluetooth_1"])
        self.assertEqual([c.pulse_source for c in chosen],
                         ["alsa_input.usb-MicA","alsa_input.usb-MicB","bluez_input.AA_BB_CC"])
        with patch("alfred.jetson.discovery.probe_microphone",return_value=True):
            limited=discover_mics(limit=2,pulse_sources=inputs)
        self.assertEqual(len(limited),2)

    def test_failed_capture_probe_is_excluded(self):
        inputs=[
            {"name":"alsa_input.usb-A","properties":{"device.bus":"usb"}},
            {"name":"bluez_input.B","monitor_of_sink":None}]
        with patch("alfred.jetson.discovery.probe_microphone",
                   side_effect=[False,True]):
            selected=discover_mics(pulse_sources=inputs)
        self.assertEqual(len(selected),1)
        self.assertEqual(selected[0].speaker,"Bluetooth_1")

    def test_empty_mics_does_not_use_fake_builtin_or_playback(self):
        listed=[{"name":"alsa_input.platform","properties":{"device.description":"Built-in Audio"}},
                {"name":"alsa_output.monitor","monitor_of_sink":10}]
        self.assertEqual(discover_mics(pulse_sources=listed),[])
        with self.assertRaises(ValueError):
            discover_mics(limit=MAX_MICS+1,pulse_sources=listed)

    def test_cli_allows_auto_or_manual_mics(self):
        parser=build_parser()
        base=["--ssd","/media/ssd","meeting","--consent","--seconds","30"]
        self.assertIsNone(parser.parse_args(base).mic)
        self.assertEqual(parser.parse_args(base+["--mic","Alice=alsa_input.usb-a"]).mic,
                         ["Alice=alsa_input.usb-a"])


class WordAlignmentTests(unittest.TestCase):
    def test_word_timestamps_are_transformed_on_common_timeline(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            one=folder/"USB_1.wav";two=folder/"USB_2.wav"
            wavfile(one,[5000]*16000)
            wavfile(two,[5000]*16000)
            records={
                "USB_1":{"source":"alsa_input.usb-a","wav":str(one),
                         "first_sample_unix_estimate":1000000.0},
                "USB_2":{"source":"alsa_input.usb-b","wav":str(two),
                         "first_sample_unix_estimate":1000000.2}}
            class Whistle:
                def transcribe(self,samples,**kwargs):
                    self.calls.append(kwargs)
                    return {"text":"We agree","words":[
                        {"word":"We","start":.12,"end":.31,"probability":.9},
                        {"word":"agree","start":.32,"end":.7,"probability":.8}]}
                calls=[]
            whistle=Whistle()
            rows=merge_channels(records,whistle=whistle,
                calibrations={"USB_2":{"method":"single_clap","scale":1.0,
                          "offset_seconds":-.2,"uncertainty_ms":100}})
            self.assertEqual(len(rows),2)
            self.assertTrue(all(r["precision"].startswith("Whistle word-level") for r in rows))
            self.assertEqual(len(rows[0]["words"]),2)
            self.assertTrue(whistle.calls[0]["word_timestamps"])
            self.assertAlmostEqual(rows[0]["start_seconds"],1000000.12,places=2)
            self.assertAlmostEqual(rows[1]["start_seconds"],1000000.12,places=2)
            self.assertTrue(rows[0]["overlap"])
            self.assertEqual(rows[1]["identity_type"],"microphone_channel")

    def test_two_distinct_overlapping_speakers_are_not_merged(self):
        rows=[
            {"id":"A","speaker":"USB_1","start_seconds":10,"end_seconds":11.6,
             "text":"Please finish the tests","energy_rms":.1,"overlap_with":[],
             "likely_crosstalk":False,"ambiguous_crosstalk":False},
            {"id":"B","speaker":"Bluetooth_1","start_seconds":10.25,"end_seconds":11.3,
             "text":"I will check it now","energy_rms":.05,"overlap_with":[],
             "likely_crosstalk":False,"ambiguous_crosstalk":False},
        ]
        annotate_overlap(rows)
        self.assertTrue(rows[0]["overlap"])
        self.assertTrue(rows[1]["overlap"])
        self.assertTrue(all(x["included_in_minutes"] for x in rows))

    def test_duplicate_crosstalk_stronger_channel_wins_but_raw_is_preserved(self):
        rows=[
            {"id":"A","speaker":"USB_1","start_seconds":10,"end_seconds":12,
             "text":"Please finish the testing","energy_rms":.12,"overlap_with":[],
             "likely_crosstalk":False,"ambiguous_crosstalk":False},
            {"id":"B","speaker":"USB_2","start_seconds":10.1,"end_seconds":11.9,
             "text":"Please finish the testing","energy_rms":.02,"overlap_with":[],
             "likely_crosstalk":False,"ambiguous_crosstalk":False},
        ]
        annotate_overlap(rows)
        self.assertFalse(rows[0]["likely_crosstalk"])
        self.assertTrue(rows[1]["likely_crosstalk"])
        self.assertFalse(rows[1]["included_in_minutes"])

    def test_short_acknowledgments_never_silently_suppressed(self):
        rows=[
            {"id":c,"speaker":c,"start_seconds":10,"end_seconds":10.3,
             "text":"yes","energy_rms":energy,"overlap_with":[],
             "likely_crosstalk":False,"ambiguous_crosstalk":False}
            for c,energy in (("A",.15),("B",.01))]
        annotate_overlap(rows)
        self.assertTrue(all(r["included_in_minutes"] for r in rows))


class CalibrationTests(unittest.TestCase):
    def test_single_clap_estimates_relative_time_offset(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            tracks={}
            for name,pos,first in [("USB_1",16000,100000.0),
                                   ("USB_2",16320,100000.0)]:
                audio=[0]*(16000*5)
                for j in range(pos,pos+160):
                    audio[j]=15000
                path=root/(name+".wav")
                wavfile(path,audio)
                tracks[name]={"wav":str(path),"source":"alsa_input.usb-a",
                              "first_sample_unix_estimate":first}
            cal=calibrate_claps(tracks)
            self.assertEqual(cal["USB_1"]["method"],"reference")
            self.assertEqual(cal["USB_2"]["method"],"single_clap")
            raw=100002.02
            shifted=adjusted_time(raw,cal["USB_2"])
            self.assertAlmostEqual(shifted,100002.0,places=2)
            self.assertIsNotNone(cal["USB_2"]["uncertainty_ms"])

    def test_two_claps_estimate_relative_clock_drift(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            records={}
            for name,second in (("USB_1",35*16000),("USB_2",35*16000+160)):
                samples=array("h",[0])* (40*16000)
                for i in (16000,second):
                    for j in range(i,i+160):
                        samples[j]=14000
                path=root/(name+".wav")
                wavfile(path,samples)
                records[name]={"wav":str(path),"source":"alsa_input.usb-a",
                               "first_sample_unix_estimate":100000.0}
            cal=calibrate_claps(records,require_two=True)
            self.assertEqual(cal["USB_2"]["method"],"two_clap_offset_and_drift")
            self.assertLess(cal["USB_2"]["scale"],1.0)
            self.assertAlmostEqual(
                adjusted_time(100000.+35.01,cal["USB_2"]),100000.+35,
                delta=.025)

    def test_calibration_fails_honestly_without_claps(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            records={}
            for name in ("USB_1","USB_2"):
                path=folder/(name+".wav")
                wavfile(path,[200]*32000)
                records[name]={"wav":str(path),"source":"alsa_input.usb-mic",
                               "first_sample_unix_estimate":100000.0}
            cal=calibrate_claps(records,require_two=True)
            self.assertEqual(cal["USB_2"]["method"],"uncalibrated")
            self.assertIsNone(cal["USB_2"]["uncertainty_ms"])


class MeetingCLIIntegrationTests(unittest.TestCase):
    def test_auto_discovery_feeds_all_working_mics_to_parallel_recorder(self):
        from alfred.jetson.cli import main
        sources=[Source("USB_1","alsa_input.usb_1"),
                 Source("USB_2","alsa_input.usb_2"),
                 Source("Bluetooth_1","bluez_input.1")]
        with patch("alfred.jetson.cli.GuardedSSD"),\
             patch("alfred.jetson.cli.discover_mics",return_value=sources) as discover,\
             patch("alfred.jetson.cli.Recorder") as record,\
             patch("alfred.jetson.cli.process_session",return_value={"pdf":"minutes.pdf"}):
            record.return_value.record.return_value=("/ssd/meeting",{})
            result=main(["--ssd","/ssd","meeting","--consent",
                         "--seconds","15","--max-mics","8"])
        self.assertEqual(result,0)
        discover.assert_called_once_with(limit=8)
        self.assertEqual(record.call_args.args[1],sources)

    def test_no_input_fails_without_recording(self):
        from alfred.jetson.cli import main
        with patch("alfred.jetson.cli.GuardedSSD"),\
             patch("alfred.jetson.cli.discover_mics",return_value=[]),\
             patch("alfred.jetson.cli.Recorder") as record:
            result=main(["--ssd","/ssd","meeting","--consent","--seconds","10"])
        self.assertEqual(result,2)
        record.assert_not_called()

class LivePreviewTests(unittest.TestCase):
    def test_live_preview_displays_channels_with_bounded_asr(self):
        from alfred.jetson.live import LiveMeetingPreview
        class FakeWhistle:
            def transcribe(self,samples,**kwargs):
                return {"text":"Meeting preview","words":[]}
        screen=io.StringIO()
        worker=LiveMeetingPreview(["USB_1","Bluetooth_1"],
                                  whistle=FakeWhistle(),stream=screen,max_pending=4)
        worker.start()
        pcm=array("h",[5000]*32000).tobytes()
        worker.submit("USB_1",pcm,0,1760000000.)
        worker.submit("Bluetooth_1",pcm,32000,1760000000.)
        report=worker.close()
        self.assertEqual(report["processed"],2)
        self.assertEqual(report["dropped"],0)
        self.assertIn("[USB_1 @",screen.getvalue())
        self.assertIn("[Bluetooth_1 @",screen.getvalue())

    def test_live_preview_capture_callback_uses_no_native_model_if_disabled(self):
        from alfred.jetson.live import LiveMeetingPreview
        worker=LiveMeetingPreview(["USB_1"],whistle=Mock(),max_pending=1)
        with self.assertRaises(ValueError):
            LiveMeetingPreview(["USB_1"],max_pending=100)
        # Nonblocking submit silently drops overflow. No unbounded audio RAM.
        worker.submit("USB_1",b"1"*64000,0,1.)
        worker.submit("USB_1",b"2"*64000,0,1.)
        self.assertEqual(worker.dropped,1)
        worker.queue.get_nowait()
        worker.queue.task_done()

class CLIViewTests(unittest.TestCase):
    def test_nonterminal_logs_each_confirmed_mic_segment_only(self):
        stream=io.StringIO()
        view=MeetingDisplay(["USB_1","USB_2"],stream=stream)
        view.event("USB_1",{"text":"Hello","start_utc":"2026-10-14T10:00:00Z"})
        view.event("USB_2",{"text":"Also speaking","start_utc":"2026-10-14T10:00:00Z"})
        view.finish()
        output=stream.getvalue()
        self.assertIn("2 microphone channels",output)
        self.assertIn("[USB_1 @",output)
        self.assertIn("[USB_2 @",output)
        self.assertEqual(output.count("Also speaking"),1)

    def test_tty_updates_existing_per_channel_row(self):
        class FakeTTY(io.StringIO):
            def isatty(self):return True
        stream=FakeTTY()
        view=MeetingDisplay(["USB_1","Bluetooth_1"],stream=stream)
        view.event("Bluetooth_1",{"text":"there","start_utc":"x"})
        view.event("Bluetooth_1",{"text":"now","start_utc":"y"})
        view.finish()
        self.assertEqual(stream.getvalue().count("waiting for Whistle"),2)
        self.assertIn("\x1b[1A",stream.getvalue())
        self.assertIn("there now",stream.getvalue())


if __name__=="__main__":
    unittest.main()
