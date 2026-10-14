"""Backend routing tests use mocks; never start real model daemons."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from alfred.backends import ModelSupervisor, backend_kind
from alfred.config import load_config
from alfred.models import Qwen


class BackendConfigTests(unittest.TestCase):
    def _load(self, qwen):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"qwen": qwen}))
            return load_config(path)["qwen"]

    def test_default_uses_new_llama_command(self):
        q = load_config()["qwen"]
        self.assertEqual(backend_kind(q), "llama_cpp")
        self.assertEqual(q["url"], "http://127.0.0.1:8080")

    def test_legacy_binary_and_url_preserved(self):
        q = self._load({"binary": "/opt/llama.cpp/build/bin/llama-server",
                        "url": "http://127.0.0.1:8888"})
        self.assertEqual(backend_kind(q), "llama_server")
        self.assertEqual(q["url"], "http://127.0.0.1:8888")

    def test_url_and_binary_inference(self):
        self.assertEqual(backend_kind(self._load({"binary": "ollama"})), "ollama")
        self.assertEqual(self._load({"binary": "ollama"})["url"], "http://127.0.0.1:11434")
        self.assertEqual(self._load({"backend": "lm_studio"})["url"], "http://127.0.0.1:1234")
        self.assertEqual(backend_kind(self._load({"url": "http://localhost:1234"})), "lm_studio")

    def test_nonloopback_and_bad_backend_rejected(self):
        for qwen in ({"url": "http://192.168.1.3:11434"},
                     {"url": "http://user@localhost:11434"},
                     {"url": "http://localhost:8080/v1"},
                     {"backend": "other"}):
            with self.subTest(qwen=qwen), self.assertRaises(ValueError):
                self._load(qwen)


class BackendSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def supervisor(self, backend, **updates):
        q = load_config()["qwen"].copy()
        q.update({"backend": backend, "model": "qwen3.5:4b" if backend == "ollama" else "",
                  "expected_model": ""})
        q.update(updates)
        return ModelSupervisor(q, self.temp.name)

    def test_llama_serve_hf_start(self):
        sup = self.supervisor("llama_cpp", binary="llama",
                              model="bartowski/Qwen_Qwen3.5-4B-GGUF:Q4_K_M")
        argv = sup._start_command("/usr/bin/llama")
        self.assertEqual(argv[:4], ["/usr/bin/llama", "serve", "-hf",
                                     "bartowski/Qwen_Qwen3.5-4B-GGUF:Q4_K_M"])
        self.assertIn("--ctx-size", argv)
        self.assertIn("--n-gpu-layers", argv)

    def test_source_directory_can_find_llama_binary(self):
        base = Path(self.temp.name) / "llama.cpp"
        target = base / "build/bin/llama"
        target.parent.mkdir(parents=True)
        target.write_text("#!/bin/sh\nexit 0\n")
        target.chmod(0o755)
        sup = self.supervisor("llama_cpp", binary=str(base))
        self.assertEqual(sup._binary(), str(target))

    def test_old_llama_server_uses_local_gguf(self):
        gguf = Path(self.temp.name) / "model.gguf"
        gguf.write_bytes(b"gguf")
        sup = self.supervisor("llama_server", binary="llama-server", model_path=str(gguf))
        cmd = sup._start_command("/usr/local/bin/llama-server")
        self.assertNotIn("serve", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], str(gguf))

    def test_ollama_start_and_no_gguf_required(self):
        sup = self.supervisor("ollama")
        self.assertEqual(sup._start_command("/usr/bin/ollama"), ["/usr/bin/ollama", "serve"])
        proc = MagicMock(pid=3)
        proc.poll.return_value = None
        with patch.object(sup, "health", side_effect=["down", "ready"]), \
             patch.object(sup, "port_open", return_value=False), \
             patch.object(sup, "verify_model", return_value="qwen3.5:4b"), \
             patch("alfred.backends.shutil.which", return_value="/usr/bin/ollama"), \
             patch("alfred.backends.subprocess.Popen", return_value=proc) as spawn:
            self.assertTrue(sup.ensure_running())
        self.assertEqual(spawn.call_args.kwargs["env"]["OLLAMA_HOST"], "127.0.0.1:8080")

    def test_lm_studio_cli_load_is_noninteractive(self):
        sup = self.supervisor("lm_studio", model="qwen-4b")
        with patch.object(sup, "health", side_effect=["down", "ready"]), \
             patch.object(sup, "port_open", return_value=False), \
             patch.object(sup, "verify_model", return_value="qwen-4b"), \
             patch("alfred.backends.shutil.which", return_value="/usr/bin/lms"), \
             patch("alfred.backends.subprocess.run") as run:
            self.assertTrue(sup.ensure_running())
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][:3],
                         ["/usr/bin/lms", "server", "start"])
        self.assertEqual(run.call_args_list[1].args[0],
                         ["/usr/bin/lms", "load", "qwen-4b", "--yes"])
        self.assertIsNone(sup.process)

    def test_adoption_does_not_spawn(self):
        sup = self.supervisor("ollama")
        with patch.object(sup, "health", return_value="ready"), \
             patch.object(sup, "verify_model", return_value="qwen3.5:4b"), \
             patch("alfred.backends.subprocess.Popen") as spawn:
            self.assertTrue(sup.ensure_running())
        spawn.assert_not_called()

    def test_unhealthy_occupied_port_not_replaced(self):
        sup = self.supervisor("lm_studio")
        with patch.object(sup, "health", return_value="down"), \
             patch.object(sup, "port_open", return_value=True), \
             patch("alfred.backends.subprocess.run") as run:
            with self.assertRaises(RuntimeError):
                sup.ensure_running()
        run.assert_not_called()

    def test_provider_specific_health(self):
        sup = self.supervisor("ollama")
        with patch("alfred.models.request_json", return_value={"data": []}) as request:
            self.assertEqual(sup.health(), "ready")
            self.assertEqual(request.call_args.args[0], sup.url + "/v1/models")
        sup = self.supervisor("llama_cpp")
        with patch("alfred.models.request_json", side_effect=HTTPError(
                sup.url, 503, "loading", None, None)):
            self.assertEqual(sup.health(), "loading")

    def test_model_selection_is_strict(self):
        sup = self.supervisor("ollama", model="qwen3.5:4b")
        with patch.object(sup, "_models", return_value=["llama3.2:3b", "qwen3.5:4b"]):
            self.assertEqual(sup.model_id(), "qwen3.5:4b")
        with patch.object(sup, "_models", return_value=["llama3.2:3b"]):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                sup.model_id()
        sup = self.supervisor("lm_studio", model="", expected_model="")
        with patch.object(sup, "_models", return_value=["first", "second"]):
            with self.assertRaisesRegex(RuntimeError, "Specify qwen.model"):
                sup.model_id()

    def test_llama_hf_resolved_name_and_expected_fallback(self):
        sup = self.supervisor("llama_cpp", model="bartowski/Qwen_Qwen3.5-4B-GGUF:Q4_K_M",
                              expected_model="Qwen3.5-4B")
        with patch.object(sup, "_models", return_value=["Qwen3.5-4B-Q4_K_M.gguf"]):
            self.assertEqual(sup.model_id(), "Qwen3.5-4B-Q4_K_M.gguf")


class BackendQwenTests(unittest.TestCase):
    def test_openai_schema_envelope_for_ollama_lm_studio(self):
        for backend in ("ollama", "lm_studio"):
            with self.subTest(backend=backend):
                sup = MagicMock(backend=backend)
                sup.url = "http://127.0.0.1:1234"
                sup.model_id.return_value = "qwen-4b"
                qwen = Qwen(load_config()["qwen"], sup)
                payload = {"choices": [{"message": {"content": '{"message":"ok","steps":[],"save_as":null}'}}]}
                with patch("alfred.models.request_json", return_value=payload) as request:
                    result = qwen.complete("hello", [])
                self.assertEqual(result["message"], "ok")
                url, body = request.call_args.args[:2]
                self.assertEqual(url, sup.url + "/v1/chat/completions")
                self.assertEqual(body["model"], "qwen-4b")
                self.assertEqual(body["response_format"]["json_schema"]["name"], "alfred_plan")
                self.assertEqual(body["response_format"]["json_schema"]["schema"]["type"], "object")

    def test_llama_schema_envelope_remains_compatible(self):
        sup = MagicMock(backend="llama_cpp")
        sup.url = "http://127.0.0.1:8080"
        sup.model_id.return_value = "qwen"
        qwen = Qwen(load_config()["qwen"], sup)
        with patch("alfred.models.request_json", return_value={
                "choices": [{"message": {"content": '{"message":"ok","steps":[],"save_as":null}'}}]}) as request:
            qwen.complete("hello", [])
        self.assertIn("schema", request.call_args.args[1]["response_format"])
        self.assertNotIn("json_schema", request.call_args.args[1]["response_format"])


if __name__ == "__main__":
    unittest.main()
