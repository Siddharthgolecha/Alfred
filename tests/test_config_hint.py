from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import alfred.app  # noqa: F401  (must load before alfred.cli: circular import)
from alfred import cli

HINT = "[alfred] No config.json found"


class FirstRunHintTests(unittest.TestCase):
    def _run(self, argv, create_config=False):
        with tempfile.TemporaryDirectory() as tmp:
            if create_config:
                Path(tmp, "config.json").write_text("{}")
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                out = io.StringIO()
                config = {"runtime": {"logs": str(Path(tmp, "logs"))}}
                with patch.object(cli, "load_config", return_value=config), \
                        patch.object(cli, "request_close", return_value=False), \
                        contextlib.redirect_stdout(out):
                    self.assertEqual(cli._main(argv), 0)
                return out.getvalue()
            finally:
                os.chdir(cwd)

    def test_hint_printed_when_default_config_missing(self):
        self.assertIn(HINT, self._run(["close"]))

    def test_no_hint_when_default_config_exists(self):
        self.assertNotIn(HINT, self._run(["close"], create_config=True))

    def test_no_hint_when_config_explicitly_passed(self):
        self.assertNotIn(HINT, self._run(["--config", "missing.json", "close"]))


if __name__ == "__main__":
    unittest.main()
