"""Docs checker regression tests for platform-dependent configuration defaults."""
import unittest

from scripts.check_docs import config_keys


class DocsCheckerTests(unittest.TestCase):
    def test_computed_default_values_do_not_break_key_discovery(self):
        keys = config_keys()
        self.assertIn("runtime.database", keys)
        self.assertIn("runtime.logs", keys)
        self.assertIn("audio.mode", keys)
        self.assertIn("qwen.backend", keys)
        self.assertTrue(all(item.count(".") == 1 for item in keys))


if __name__ == "__main__":
    unittest.main()
