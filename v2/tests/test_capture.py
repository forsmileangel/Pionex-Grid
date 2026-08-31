import os
import subprocess
import unittest

from v2.capture import _collector_run_kwargs


class CaptureRunnerTests(unittest.TestCase):
    def test_windows_collector_hides_console(self):
        if os.name != "nt":
            self.skipTest("Windows-only console flag")
        kwargs = _collector_run_kwargs()
        self.assertEqual(kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        self.assertTrue(kwargs["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW)
