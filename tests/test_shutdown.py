"""Linux shutdown acceptance tests; do not require Cactus, microphone, or a GGUF."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import threading
import tempfile
import time
import unittest
from unittest.mock import patch

from alfred.backends import ModelSupervisor
from alfred.config import load_config
from alfred.lifecycle import ChildProcesses, ShutdownSocket, control_dir, request_close


@unittest.skipUnless(sys.platform.startswith("linux"), "Unix socket and process group tests")
class ShutdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.logdir = self.root / "state"
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "runtime": {"database": str(self.root / "alfred.db"),
                        "logs": str(self.logdir)},
            "qwen": {"enabled": False},
        }))
        self.child = None

    def tearDown(self):
        if self.child is not None and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=5)

    def start_scheduler(self):
        self.child = subprocess.Popen(
            [sys.executable, "-m", "alfred", "--config", str(self.config),
             "--scheduler-only"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        path = control_dir(self.logdir) / "scheduler.sock"
        deadline = time.monotonic() + 10
        while not path.exists() and self.child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.025)
        self.assertIsNone(self.child.poll(), "Headless process exited before startup")
        self.assertTrue(path.exists(), "Shutdown control socket was not created")
        return path

    def test_close_command_exits_scheduler_and_cleans_socket(self):
        path = self.start_scheduler()
        result = subprocess.run(
            [sys.executable, "-m", "alfred", "--config", str(self.config), "close"],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scheduler", result.stdout)
        self.assertEqual(self.child.wait(timeout=5), 0)
        self.assertFalse(path.exists())
        self.assertEqual(subprocess.run(
            [sys.executable, "-m", "alfred", "--config", str(self.config), "close"],
            capture_output=True, timeout=5,
        ).returncode, 0)

    def test_sigint_also_closes_scheduler(self):
        path = self.start_scheduler()
        self.child.send_signal(signal.SIGINT)
        self.assertEqual(self.child.wait(timeout=8), 0)
        self.assertFalse(path.exists())

    def test_sigterm_also_closes_scheduler(self):
        path = self.start_scheduler()
        self.child.send_signal(signal.SIGTERM)
        self.assertEqual(self.child.wait(timeout=8), 0)
        self.assertFalse(path.exists())

    def test_sigint_during_socket_entry_removes_just_created_socket(self):
        # Deterministically simulate a signal after bind(), before __enter__
        # has returned to the with statement's exception cleanup.
        path = control_dir(self.logdir) / "scheduler.sock"
        with patch("alfred.lifecycle.threading.Thread.start",
                   side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                with ShutdownSocket(self.logdir, "scheduler"):
                    self.fail("Interrupted entry should not yield")
        self.assertFalse(path.exists())
        # The orphan cleanup must not prevent a subsequent safe start.
        with ShutdownSocket(self.logdir,"scheduler"):
            self.assertTrue(path.exists())
        self.assertFalse(path.exists())

    def test_single_instance_and_private_socket(self):
        with ShutdownSocket(self.logdir, "voice") as active:
            self.assertEqual(stat.S_IMODE(active.path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(active.path.stat().st_mode), 0o600)
            with self.assertRaisesRegex(RuntimeError, "already running"):
                with ShutdownSocket(self.logdir, "voice"):
                    pass
        self.assertFalse(active.path.exists())

    def test_stale_socket_is_cleaned(self):
        # Crash leaves a dead socket, not a live owner; safe to reclaim.
        from socket import socket, AF_UNIX, SOCK_STREAM
        directory = control_dir(self.logdir)
        path = directory / "voice.sock"
        sock = socket(AF_UNIX, SOCK_STREAM)
        sock.bind(str(path))
        sock.close()
        with ShutdownSocket(self.logdir, "voice") as active:
            self.assertTrue(active.path.exists())
        self.assertFalse(path.exists())

    def test_regular_file_at_socket_path_is_not_deleted(self):
        path = control_dir(self.logdir) / "voice.sock"
        path.write_text("Do not remove me")
        with self.assertRaises(PermissionError):
            with ShutdownSocket(self.logdir, "voice"):
                pass
        self.assertEqual(path.read_text(), "Do not remove me")

    def test_owned_tool_child_is_cancelled_on_shutdown(self):
        registry = ChildProcesses()
        started = threading.Event()
        result = []
        def worker():
            started.set()
            result.append(registry.run(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                timeout=65, capture_output=True, text=True,
            ))
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self.assertTrue(started.wait(2))
        deadline = time.monotonic() + 5
        while not registry._children and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(registry._children)
        process = next(iter(registry._children))
        registry.close()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(process.poll())
        self.assertEqual(result[0].returncode, -signal.SIGTERM)
        with self.assertRaisesRegex(RuntimeError, "shutting down"):
            registry.run([sys.executable, "-c", "pass"], timeout=2)

    def test_named_task_timeout_reaps_child(self):
        registry = ChildProcesses()
        with self.assertRaises(subprocess.TimeoutExpired):
            registry.run([sys.executable, "-c", "import time; time.sleep(60)"],
                         timeout=0.1, capture_output=True, text=True)
        self.assertFalse(registry._children)
        registry.close()

    def test_managed_model_terminates_its_process_group(self):
        cfg = load_config()["qwen"]
        cfg["enabled"] = True
        supervisor = ModelSupervisor(cfg, self.logdir)
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            supervisor.process = child
            supervisor.started_here = True
            supervisor.close()
            self.assertIsNotNone(child.poll())
            self.assertFalse(supervisor.started_here)
            supervisor.close()  # repeat must be harmless
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def test_adopted_model_is_never_killed(self):
        cfg = load_config()["qwen"]
        supervisor = ModelSupervisor(cfg, self.logdir)
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            supervisor.process = child
            supervisor.started_here = False
            supervisor.close()
            self.assertIsNone(child.poll())
        finally:
            child.terminate()
            child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
