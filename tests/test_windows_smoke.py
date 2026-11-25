import importlib.util
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4


@unittest.skipUnless(sys.platform == "win32", "Requires Windows and the project dependencies")
class WindowsSmokeTests(unittest.TestCase):
    def test_second_process_sees_mutex(self):
        import tracker

        name = f"FocusCoach_Test_{uuid4().hex}"
        with patch.object(tracker, "MUTEX_NAME", name):
            self.assertFalse(tracker.another_copy_is_running())
            try:
                child = subprocess.run(
                    [
                        sys.executable, "-c",
                        "import sys, tracker; tracker.MUTEX_NAME = sys.argv[1]; "
                        "sys.exit(0 if tracker.another_copy_is_running() else 1)",
                        name,
                    ],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(child.returncode, 0, child.stderr)
            finally:
                tracker._instance_mutex.Close()
                tracker._instance_mutex = None

    def test_optional_media_bindings_import(self):
        if importlib.util.find_spec("winrt") is None:
            self.skipTest("Install requirements-media.txt to check the optional bindings")
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager

        self.assertTrue(callable(GlobalSystemMediaTransportControlsSessionManager.request_async))
