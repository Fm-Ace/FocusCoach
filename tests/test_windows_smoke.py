import importlib.util
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4


@unittest.skipUnless(sys.platform == "win32", "Requires Windows and the project dependencies")
class WindowsSmokeTests(unittest.TestCase):
    def test_second_process_uses_launcher_to_request_stop(self):
        import tracker

        mutex_name = f"FocusCoach_Test_{uuid4().hex}"
        event_name = f"FocusCoach_Stop_Test_{uuid4().hex}"
        with (
            patch.object(tracker, "MUTEX_NAME", mutex_name),
            patch.object(tracker, "STOP_EVENT_NAME", event_name),
        ):
            self.assertFalse(tracker.another_copy_is_running())
            try:
                child = subprocess.run(
                    [
                        sys.executable, "-c",
                        "import sys, run_focuscoach; "
                        "run_focuscoach.STOP_EVENT_NAME = sys.argv[1]; "
                        "sys.exit(0 if run_focuscoach.request_stop() else 1)",
                        event_name,
                    ],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(child.returncode, 0, child.stderr)
                self.assertTrue(tracker.stop_requested())
            finally:
                tracker.close_instance_handles()

    def test_optional_media_bindings_import(self):
        if importlib.util.find_spec("winrt") is None:
            self.skipTest("Install requirements-media.txt to check the optional bindings")
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager

        self.assertTrue(callable(GlobalSystemMediaTransportControlsSessionManager.request_async))


if __name__ == "__main__":
    unittest.main()
