import unittest
from unittest.mock import patch

import run_focuscoach


class LauncherTests(unittest.TestCase):
    def test_existing_tracker_receives_stop_without_spawning_another(self):
        with (
            patch.object(run_focuscoach, "request_stop", return_value=True),
            patch.object(run_focuscoach.subprocess, "Popen") as popen,
        ):
            self.assertEqual(run_focuscoach.main(), 0)

        popen.assert_not_called()

    def test_new_tracker_is_spawned_and_launcher_waits_for_ready_signal(self):
        with (
            patch.object(run_focuscoach, "request_stop", return_value=False),
            patch.object(run_focuscoach.subprocess, "Popen") as popen,
            patch.object(run_focuscoach, "wait_for_start", return_value=True) as wait_for_start,
        ):
            self.assertEqual(run_focuscoach.main(), 0)

        popen.assert_called_once()
        wait_for_start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
