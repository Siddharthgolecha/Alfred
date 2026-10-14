"""Wake-word security regressions. No native Cactus or Jetson hardware required."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from alfred.config import load_config
from alfred.wakeword import AcousticWakeDetector, WakeWordGate


class WakeWordGateTests(unittest.TestCase):
    def setUp(self):
        self.settings = load_config()["wake_word"]
        self.gate = WakeWordGate(self.settings)

    def test_wake_is_on_by_default(self):
        self.assertTrue(self.gate.enabled)
        self.assertEqual(self.gate.mode, "transcript")
        self.assertEqual(self.gate.phrase, "Alfred")

    def test_unrelated_speech_does_not_activate(self):
        for text in ("Check GPU", "tell Alfred about the weather",
                     "my friend Alfred said hello", "Alfredson start timer",
                     "not Alfred check GPU"):
            with self.subTest(text=text):
                self.assertIsNone(self.gate.preview(text))
                self.assertIsNone(self.gate.consume(text, now=100))

    def test_wake_phrase_is_stripped_from_command(self):
        for voice in ("Alfred, check GPU status", "hey alfred check GPU status",
                      "OK Alfred: check GPU status", "ALFRED! check GPU status"):
            with self.subTest(voice=voice):
                self.assertEqual(self.gate.consume(voice, now=100),
                                 "check GPU status")

    def test_standalone_wake_arms_one_followup(self):
        self.assertEqual(self.gate.consume("Hey, Alfred!", now=100), "")
        self.assertTrue(self.gate.is_armed(now=104))
        self.assertEqual(self.gate.preview("check GPU",now=104), "check GPU")
        self.assertEqual(self.gate.consume("check GPU", now=104), "check GPU")
        self.assertIsNone(self.gate.consume("stop service",now=105))

    def test_expiration_does_not_accept_a_late_utterance(self):
        self.assertEqual(self.gate.consume("Alfred",now=100),"")
        self.assertIsNone(self.gate.consume("check GPU",now=109))
        self.assertFalse(self.gate.is_armed(now=109))

    def test_a_second_standalone_wake_renews_window(self):
        self.assertEqual(self.gate.consume("Alfred",now=100),"")
        self.assertEqual(self.gate.consume("Alfred",now=107),"")
        self.assertEqual(self.gate.consume("status",now=114),"status")

    def test_acoustic_trigger_allows_command_without_prefix(self):
        self.assertEqual(
            self.gate.consume("Check the GPU", acoustic_activation=True,now=100),
            "Check the GPU")
        self.assertIsNone(self.gate.consume("Unrelated speech",now=101))

    def test_acoustic_wake_with_empty_transcript_arms_followup(self):
        self.assertEqual(self.gate.consume("",acoustic_activation=True,now=100),"")
        self.assertEqual(self.gate.consume("Status",now=103),"Status")

    def test_disabled_gate_keeps_explicit_legacy_mode(self):
        self.settings["enabled"]=False
        g=WakeWordGate(self.settings)
        self.assertEqual(g.consume("GPU status"),"GPU status")

    def test_custom_multiword_literal_phrase(self):
        self.settings["phrase"]="Computer Alfred"
        g=WakeWordGate(self.settings)
        self.assertEqual(g.consume("Hey computer Alfred, hello"),"hello")
        self.assertIsNone(g.consume("Hey Alfred, hello"))


class WakeConfigurationTests(unittest.TestCase):
    def test_reject_bad_configuration(self):
        for invalid in (
            {"mode":"always_on"},
            {"phrase":""},
            {"phrase":"Alfred|anything"},
            {"enabled":"false"},
            {"followup_seconds":31},
            {"followup_seconds":-1},
            {"threshold":1.5},
            {"model_path":123},
        ):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/"config.json"
                path.write_text(json.dumps({"wake_word":invalid}))
                with self.assertRaises(ValueError):
                    load_config(path)

    def test_acoustic_missing_model_fails_closed(self):
        cfg=load_config()["wake_word"]
        cfg["mode"]="openwakeword"
        cfg["model_path"]="/missing/alfred.onnx"
        with self.assertRaisesRegex(RuntimeError,"requires a local custom model"):
            AcousticWakeDetector(cfg)


class AlfredVoiceTests(unittest.TestCase):
    def run_voice(self, transcripts, *, mode="transcript", enabled=True,
                  detector=None):
        from alfred.app import Alfred
        cfg=load_config()
        cfg["wake_word"]["mode"]=mode
        cfg["wake_word"]["enabled"]=enabled
        with tempfile.TemporaryDirectory() as tmp:
            cfg["runtime"]["database"]=str(Path(tmp)/"db.sqlite")
            cfg["runtime"]["logs"]=tmp
            app=Alfred(cfg,load_models=False)
            app.cactus=Mock()
            utterances=iter(transcripts)

            def transcribe(chunks,on_partial):
                transcript=next(utterances)
                on_partial(transcript)
                return transcript

            app.cactus.transcribe_stream.side_effect=transcribe
            app.handle_text=Mock()
            class FakeUtterances:
                def __init__(self,*args,**kwargs):
                    pass
                def utterances(self):
                    return iter([iter([[0.0]]) for _ in transcripts])
            out=io.StringIO()
            try:
                with patch("alfred.app.UtteranceStream",FakeUtterances), \\
                     patch("sys.stdout",out):
                    if detector is not None:
                        with patch("alfred.app.AcousticWakeDetector",return_value=detector):
                            app.listen()
                    else:
                        app.listen()
                return [x.args[0] for x in app.handle_text.call_args_list], \\
                       out.getvalue(), app.cactus.transcribe_stream.call_count
            finally:
                app.close()
                app.store.db.close()

    def test_unaddressed_conversation_does_not_execute_or_print(self):
        commands,output,asr_count=self.run_voice(
            ["what's the GPU status", "tell Alfred hi", "thank you"])
        self.assertEqual(commands,[])
        self.assertEqual(output,"")
        self.assertEqual(asr_count,3)  # transcript mode still performs local STT

    def test_wake_command_and_standalone_wake_followup(self):
        commands,output,count=self.run_voice(
            ["Alfred, check GPU status", "not a command", "Alfred", "list reminders",
             "stop service"])
        self.assertEqual(commands,["check GPU status","list reminders"])
        self.assertIn("Whistle: Alfred, check GPU status",output)
        self.assertIn("Whistle: Alfred",output)
        self.assertNotIn("stop service",output)

    def test_explicit_opt_out_for_legacy_voice_deployment(self):
        commands,_,_=self.run_voice(["GPU status"], enabled=False)
        self.assertEqual(commands,["GPU status"])

    def test_acoustic_mode_runs_asr_only_after_detector_hit(self):
        detector=Mock()
        detector.capture_after_wake.side_effect=[
            None, iter([[0.1]*8000]), None
        ]
        commands,output,asr_count=self.run_voice(
            ["check GPU"],mode="openwakeword",detector=detector)
        # The mocked utterance source uses one segment per transcript,
        # so supply three slots to exercise ignored / accepted / ignored.
        self.assertEqual(commands,["check GPU"])
        self.assertEqual(asr_count,1)
        self.assertEqual(detector.capture_after_wake.call_count,3)
        self.assertIn("Whistle: check GPU",output)


class AcousticFrameTests(unittest.TestCase):
    def test_pcm_frames_detect_numpy_floats_and_replay_context(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("optional numpy is not installed")
        module=types.ModuleType("openwakeword")
        module.__path__=[]
        model_module=types.ModuleType("openwakeword.model")
        fake=Mock()
        fake.predict.return_value={"alfred":np.float32(0.82)}
        model_module.Model=Mock(return_value=fake)
        cfg=load_config()["wake_word"]
        cfg["model_path"]=""
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"alfred.onnx"
            path.write_bytes(b"unit test only")
            cfg["model_path"]=str(path)
            with patch.dict(sys.modules,{"openwakeword":module,
                                         "openwakeword.model":model_module}):
                detector=AcousticWakeDetector(cfg)
            frames=[([0.1]*8000),([0.2]*8000)]
            captured=detector.capture_after_wake(iter(frames),500)
            self.assertEqual(list(captured),frames)
            self.assertTrue(fake.predict.called)
            self.assertEqual(len(fake.predict.call_args.args[0]),1280)


if __name__=="__main__":
    unittest.main()
