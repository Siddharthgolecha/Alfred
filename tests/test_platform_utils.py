import unittest
from unittest.mock import patch

from alfred import platform_utils


class MacResourceSamplingTests(unittest.TestCase):
    def test_available_memory_uses_page_size_reported_by_vm_stat(self):
        vm_stat = (
            "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
            "Pages free:                               100.\n"
            "Pages inactive:                          200.\n"
        )
        with patch.object(platform_utils, "IS_MACOS", True), \
                patch.object(platform_utils, "IS_LINUX", False), \
                patch.object(platform_utils, "IS_WINDOWS", False), \
                patch.object(platform_utils.subprocess, "check_output",
                             side_effect=[vm_stat, "total = 9216.00M used = 1024.00M free = 8192.00M"]), \
                patch.object(platform_utils.os, "getloadavg", return_value=(1.0, 1.0, 1.0)), \
                patch.object(platform_utils.os, "cpu_count", return_value=2):
            sample = platform_utils.sample_resources()

        self.assertEqual(sample["available_mib"], 300 * 16384 / (1024 * 1024))
        self.assertEqual(sample["swap_used_mib"], 1024.0)
        self.assertEqual(sample["cpu_load_per_core"], 0.5)


if __name__ == "__main__":
    unittest.main()
