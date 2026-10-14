"""Unit tests requiring no Jetson hardware, GGUF weights, or cactus-native engine."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

from alfred.audio import AudioManager, UtteranceStream, rms
from alfred.config import load_config
from alfred.models import LlamaSupervisor, Qwen
from alfred.runtime import Store, Tools, validate, validate_plan, Scheduler

class ConfigTests(unittest.TestCase):
    def test_allowlisted_named_tasks_support_custom_names(self):
        with tempfile.TemporaryDirectory() as temp:
            conf = Path(temp)/"config.json"
            conf.write_text(json.dumps({"runtime":{"named_tasks":{"hello":["/bin/echo","hello"]}}}))
            result=load_config(conf)
            self.assertEqual(result["runtime"]["named_tasks"]["hello"][0],"/bin/echo")

    def test_nonloopback_llama_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            conf = Path(temp)/"config.json"
            conf.write_text(json.dumps({"qwen":{"url":"http://example.org:8080"}}))
            with self.assertRaises(ValueError):
                load_config(conf)

    def test_unknown_config_key_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            conf = Path(temp)/"config.json"
            conf.write_text(json.dumps({"runtime":{"run_any_command":True}}))
            with self.assertRaises(ValueError):
                load_config(conf)

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/"alfred.db")
        self.tools=Tools({"allowed_services":[],"named_tasks":{},"max_plan_steps":5},self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_reminder_claimed_once(self):
        new=self.store.create_reminder("check GPU",1)
        self.store.db.execute("UPDATE reminders SET due_at=? WHERE id=?",
                              (int(time.time())-1,new["id"]))
        self.store.db.commit()
        claimed=self.store.due()
        self.assertEqual(len(claimed),1)
        self.assertEqual(claimed[0][0],"reminder")
        self.assertEqual(self.store.due(),[])
        self.assertEqual(self.store.list_reminders(),[])

    def test_recurring_schedule_rearmed_and_claimed_once(self):
        new=self.store.create_schedule("get_system_status",2)
        self.store.db.execute("UPDATE schedules SET due_at=? WHERE id=?",
                              (int(time.time())-5,new["id"]))
        self.store.db.commit()
        self.assertEqual(len(self.store.due()),1)
        self.assertEqual(self.store.due(),[])
        self.assertGreater(self.store.list_schedules()[0]["due_at"],int(time.time()))

    def test_forbid_nonallowlisted_script_or_service(self):
        with self.assertRaises(PermissionError):
            self.tools.execute("run_named_task",{"name":"rm"})
        with self.assertRaises(PermissionError):
            self.tools.execute("control_service",{"name":"ssh.service","action":"stop"})

    def test_invalid_parameters_rejected(self):
        with self.assertRaises(ValueError):
            validate("create_reminder",{"message":"hello","delay_minutes":"10"})
        with self.assertRaises(ValueError):
            validate("get_gpu_status",{"path":"/etc/passwd"})
        with self.assertRaises(ValueError):
            validate("random_shell",{})

    def test_plan_condition(self):
        plan=[
            {"tool":"get_gpu_status","args":{}},
            {"tool":"get_system_status","args":{},
             "when":{"step":0,"field":"gpu_temp_c","op":"gt","value":80}}
        ]
        self.assertEqual(len(validate_plan(plan)),2)
        plan[1]["when"]["step"]=1
        with self.assertRaises(ValueError):
            validate_plan(plan)

    def test_no_recursive_workflow(self):
        with self.assertRaises(ValueError):
            validate_plan([{"tool":"run_workflow","args":{"name":"loop"}}])

    def test_save_and_execute_workflow(self):
        self.store.save_workflow("health",[{"tool":"get_system_status","args":{}}])
        result=self.tools.execute("run_workflow",{"name":"health"})
        self.assertIn("memory_mib",result[0])

class AudioTests(unittest.TestCase):
    def setUp(self):
        self.cfg=load_config()["audio"]
        self.manager=AudioManager(self.cfg)

    @patch("alfred.audio.command")
    @patch("alfred.audio.pactl_json")
    def test_sony_preferred_over_direct(self,mock_pactl,mock_cmd):
        card={"name":"bluez_card.AA_BB_CC_DD_EE_FF",
              "properties":{"device.description":"Sony WI-C100"},
              "active_profile":{"name":"a2dp-sink"},
              "profiles":[{"name":"headset-head-unit","available":"yes"}]}
        src=[{"name":"alsa_input.usb_headset","monitor_of_sink":None},
             {"name":"bluez_input.AA_BB_CC_DD_EE_FF.0","monitor_of_sink":None}]
        mock_pactl.side_effect=lambda x: [card] if x=="cards" else src
        self.assertEqual(self.manager.choose_source(),"bluez_input.AA_BB_CC_DD_EE_FF.0")
        mock_cmd.assert_called_with("pactl","set-card-profile",card["name"],"headset-head-unit")

    @patch("alfred.audio.pactl_json")
    def test_direct_fallback(self,mock_pactl):
        mock_pactl.side_effect=lambda x: [] if x=="cards" else [{"name":"alsa_input.usb_mic","monitor_of_sink":None}]
        self.assertEqual(self.manager.choose_source(),"alsa_input.usb_mic")
        self.cfg["mode"]="bluetooth"
        self.assertIsNone(self.manager.choose_source())

    def test_vad_emits_incremental_utterance(self):
        cfg=dict(self.cfg,speech_rms=.01,frame_ms=500,end_silence_frames=2)
        quiet=[0.0]*8000
        voice=[0.05]*8000
        frames=iter([quiet,voice,voice,quiet,quiet,quiet,quiet,voice,quiet,quiet])
        segments=UtteranceStream(frames,cfg).utterances()
        first=list(next(segments))
        second=list(next(segments))
        self.assertGreaterEqual(len(first),4)
        self.assertGreaterEqual(len(second),3)
        self.assertGreater(rms(first[1]),0.01)

class ModelTests(unittest.TestCase):
    def test_adopt_healthy_llama_server(self):
        cfg=load_config()["qwen"]
        with tempfile.TemporaryDirectory() as temp:
            sup=LlamaSupervisor(cfg,temp)
            with patch.object(sup,"health",return_value="ready"), \
                 patch.object(sup,"model_id",return_value="Qwen3.5-4B-Q4_K_M.gguf"), \
                 patch("alfred.models.subprocess.Popen") as spawn:
                self.assertTrue(sup.ensure_running())
                spawn.assert_not_called()

    def test_do_not_spawn_on_occupied_port(self):
        cfg=load_config()["qwen"]
        with tempfile.TemporaryDirectory() as temp:
            sup=LlamaSupervisor(cfg,temp)
            with patch.object(sup,"health",return_value="unhealthy"), \
                 patch.object(sup,"port_open",return_value=True), \
                 patch("alfred.models.subprocess.Popen") as spawn:
                with self.assertRaises(RuntimeError):
                    sup.ensure_running()
                spawn.assert_not_called()

    def test_start_missing_gguf_fails_actionably(self):
        cfg=load_config()["qwen"]
        cfg["model_path"]="/not/present/unknown-qwen.gguf"
        with tempfile.TemporaryDirectory() as temp:
            sup=LlamaSupervisor(cfg,temp)
            with patch.object(sup,"health",return_value="down"), \
                 patch.object(sup,"port_open",return_value=False):
                with self.assertRaises(FileNotFoundError):
                    sup.ensure_running()


    def test_start_missing_server_from_gguf(self):
        cfg=load_config()["qwen"]
        with tempfile.TemporaryDirectory() as temp:
            model=Path(temp)/"Qwen3.5-4B-Q4_K_M.gguf"
            model.write_bytes(b"test fixture")
            cfg["model_path"]=str(model)
            cfg["binary"]="fake-llama-server"
            sup=LlamaSupervisor(cfg,temp)
            proc=MagicMock()
            proc.pid=12345
            proc.poll.return_value=None
            with patch.object(sup,"health",side_effect=["down","ready"]), \
                 patch.object(sup,"port_open",return_value=False), \
                 patch.object(sup,"verify_model",return_value="Qwen3.5-4B"), \
                 patch("alfred.models.shutil.which",return_value="/fake/llama-server"), \
                 patch("alfred.models.subprocess.Popen",return_value=proc) as spawn:
                self.assertTrue(sup.ensure_running())
                self.assertEqual(sup.status if False else sup.process.pid,12345)
                self.assertTrue(sup.started_here)
                argv=spawn.call_args.args[0]
                self.assertIn(str(model.resolve()),argv)
                self.assertIn("--n-gpu-layers",argv)
                self.assertEqual(argv[argv.index("--host")+1],"127.0.0.1")

    def test_cactus_uses_finalized_whistle_text(self):
        import types
        from alfred.models import Cactus
        agent=MagicMock()
        agent.complete.return_value={"type":"respond","function_calls":[]}
        class FakeWhistle:
            def stream(self,frames,**kwargs):
                values=list(frames)
                self_values=len(values)
                assert self_values==2
                yield {"text":"turn","pending":"turn on the"}
                yield {"text":"on the light","pending":""}
        fake=types.SimpleNamespace(Needle=lambda tools:agent,Whistle=FakeWhistle)
        with patch.dict(sys.modules,{"needle":fake}):
            cactus=Cactus([])
        self.assertEqual(cactus.transcribe_stream([[0.0]*8000,[0.1]*8000]),"turn on the light")
        cactus.route("Check GPU")
        agent.complete.assert_called_once()

if __name__=="__main__":
    unittest.main()
