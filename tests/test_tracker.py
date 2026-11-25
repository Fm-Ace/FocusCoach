import asyncio
import importlib.util
import sys
import types
import unittest
from unittest.mock import Mock, patch


class TrackerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        names = ("psutil", "pywintypes", "win32api", "win32event", "win32gui", "win32process", "winerror")
        cls.modules = {name: types.ModuleType(name) for name in names}
        cls.modules["pywintypes"].error = OSError
        with patch.dict(sys.modules, cls.modules):
            spec = importlib.util.find_spec("tracker")
            cls.tracker = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.tracker)

    def test_mutex_error_prevents_database_startup(self):
        with patch.object(self.modules["win32event"], "CreateMutex", side_effect=OSError, create=True):
            with patch.dict(sys.modules, {"tracker": self.tracker}):
                spec = importlib.util.find_spec("focuscoach")
                app = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(app)
            with (
                patch.object(app, "ActivityLog") as log,
                patch.object(app, "setup_logging") as setup_logging,
                patch.object(app.sys, "stdout", new=object()),
                patch.object(app.sys, "stderr", new=types.SimpleNamespace(write=Mock())) as stderr,
            ):
                self.assertEqual(app.main(), 1)
                log.assert_not_called()
                setup_logging.assert_not_called()
                stderr.write.assert_any_call("Could not check whether another copy of FocusCoach is running.")

    def test_second_launch_signals_existing_instance_to_stop(self):
        event = object()
        self.tracker.STOP_EVENT_NAME = "FocusCoach_Test_Stop"

        with (
            patch.object(self.modules["win32event"], "CreateEvent", return_value=event, create=True) as create_event,
            patch.object(self.modules["win32event"], "CreateMutex", return_value=object(), create=True),
            patch.object(self.modules["win32event"], "SetEvent", create=True) as set_event,
            patch.object(self.modules["win32api"], "GetLastError", return_value=183, create=True),
            patch.object(self.modules["winerror"], "ERROR_ALREADY_EXISTS", 183, create=True),
        ):
            self.assertTrue(self.tracker.another_copy_is_running())

        create_event.assert_called_once_with(None, True, False, "FocusCoach_Test_Stop")
        set_event.assert_called_once_with(event)

    def test_playback_uses_exact_ids_and_expires_after_failed_reads(self):
        monitor = self.tracker.MediaPlaybackMonitor()
        manager = Mock()
        clock = Mock(return_value=10.0)
        results = []
        playback = types.SimpleNamespace(playback_status=1)
        playing = [
            types.SimpleNamespace(source_app_user_model_id=app_id, get_playback_info=lambda: playback)
            for app_id in ("CHROME.EXE", "Microsoft Edge")
        ]

        def get_sessions():
            if clock.return_value == 10.0:
                return playing
            raise OSError("media query failed")

        async def request_async():
            return manager

        async def advance_clock(_):
            clock.return_value += 2
            results.append(tuple(monitor.is_playing_for(app) for app in ("Chrome.exe", "MSEDGE.EXE", "rom.exe")))
            if len(results) == 2:
                monitor._stop.set()

        manager.get_sessions.side_effect = get_sessions
        media = types.ModuleType("winrt.windows.media.control")
        media.GlobalSystemMediaTransportControlsSessionManager = types.SimpleNamespace(request_async=request_async)
        media.GlobalSystemMediaTransportControlsSessionPlaybackStatus = types.SimpleNamespace(PLAYING=1)
        with (
            patch.dict(sys.modules, {media.__name__: media}),
            patch.object(self.tracker, "time", types.SimpleNamespace(monotonic=clock)),
            patch.object(self.tracker, "asyncio", types.SimpleNamespace(sleep=advance_clock)),
        ):
            asyncio.run(monitor._watch())
        self.assertEqual(results, [(True, True, False), (False, False, False)])


if __name__ == "__main__":
    unittest.main()
