"""Read the active Windows app and its window title."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time

import psutil
import pywintypes
import win32api
import win32event
import win32gui
import win32process
import winerror

from activity_log import Activity

logger = logging.getLogger(__name__)

IDLE_AFTER = 120
MUTEX_NAME = "FocusCoach_SingleInstance"
STOP_EVENT_NAME = "FocusCoach_Stop"
MAX_TITLE_LENGTH = 300
PROTECTED_APP = "Protected app"
UWP_HOST = "applicationframehost.exe"

BROWSERS = {
    "zen.exe": ("Zen", "Zen Browser"),
    "firefox.exe": ("Firefox", "Mozilla Firefox"),
    "chrome.exe": ("Chrome", "Google Chrome"),
    "msedge.exe": ("Edge", "Microsoft Edge"),
    "brave.exe": ("Brave", "Brave"),
}

MEDIA_ALIASES = {
    "msedge": ("edge", "microsoft edge"),
}

_instance_mutex = None
_stop_event = None


def another_copy_is_running() -> bool:
    """Create the shared stop signal; a second launch asks the first to stop."""
    global _instance_mutex, _stop_event

    try:
        _instance_mutex = win32event.CreateMutex(None, False, MUTEX_NAME)
        already_running = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
        _stop_event = win32event.CreateEvent(None, True, False, STOP_EVENT_NAME)

        if already_running:
            win32event.SetEvent(_stop_event)

        return already_running
    except pywintypes.error:
        close_instance_handles()
        raise RuntimeError("Could not check whether another copy of FocusCoach is running.") from None


def stop_requested() -> bool:
    """Return whether another launch has requested a graceful shutdown."""
    if _stop_event is None:
        return False

    return win32event.WaitForSingleObject(_stop_event, 0) == win32event.WAIT_OBJECT_0


def close_instance_handles() -> None:
    """Release the named Windows handles when this process is exiting."""
    global _instance_mutex, _stop_event

    for handle in (_stop_event, _instance_mutex):
        if handle is not None:
            try:
                handle.Close()
            except (AttributeError, pywintypes.error):
                pass

    _stop_event = None
    _instance_mutex = None


def clean_title(title: str) -> str:
    return " ".join(title.split())[:MAX_TITLE_LENGTH]


def get_idle_seconds() -> float:
    last_input = win32api.GetLastInputInfo()
    current_tick = win32api.GetTickCount()
    # Windows uses a 32-bit counter, so this keeps the result correct when it wraps around.
    return ((current_tick - last_input) & 0xFFFFFFFF) / 1000


def get_process_name(process_id: int) -> str:
    try:
        return psutil.Process(process_id).name()
    except psutil.AccessDenied:
        return PROTECTED_APP


def get_real_uwp_process(window: int, host_process_id: int) -> int:
    """Find the app inside Windows' shared Store-app host process."""
    # Store apps can appear as ApplicationFrameHost, so look for the real child process.
    child_processes = []

    def collect(child_window, _):
        _, process_id = win32process.GetWindowThreadProcessId(child_window)
        if process_id != host_process_id:
            child_processes.append(process_id)
        return True

    win32gui.EnumChildWindows(window, collect, None)
    return child_processes[0] if child_processes else host_process_id


def get_active_window() -> tuple[str, str] | None:
    try:
        window = win32gui.GetForegroundWindow()
        if not window:
            return None

        _, process_id = win32process.GetWindowThreadProcessId(window)
        process_name = get_process_name(process_id)

        if process_name.casefold() == UWP_HOST:
            real_id = get_real_uwp_process(window, process_id)
            process_name = get_process_name(real_id)

        title = clean_title(win32gui.GetWindowText(window)) or "Untitled window"
        return process_name, title
    except (pywintypes.error, psutil.Error):
        return None


def get_browser_title(process_name: str, title: str) -> tuple[str, str]:
    """Use the page title Windows already puts in the browser's title bar."""
    # The browser name is just a suffix; remove it to keep the actual page title.
    browser = BROWSERS.get(process_name.casefold())
    if not browser:
        return title, ""

    browser_name, suffix = browser
    pattern = rf"\s+[-—–]\s+{re.escape(suffix)}\s*$"
    page_title = re.sub(pattern, "", title, flags=re.IGNORECASE).strip()
    return page_title or title, ""


class MediaPlaybackMonitor:
    """Keep track of apps Windows currently reports as playing media."""

    # Media sessions use an async Windows API, so check them on a separate thread.

    def __init__(self, check_every: float = 1.0):
        self.check_every = check_every
        self._playing_apps: set[str] = set()
        self._playing_until = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="FocusCoach media monitor",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def is_playing_for(self, process_name: str) -> bool:
        app = process_name.casefold().removesuffix(".exe")

        with self._lock:
            return time.monotonic() < self._playing_until and (
                app in self._playing_apps
                or any(alias in self._playing_apps for alias in MEDIA_ALIASES.get(app, ()))
            )

    def _run(self) -> None:
        try:
            asyncio.run(self._watch())
        except ImportError:
            logger.warning(
                "Media detection is unavailable. Install requirements-media.txt "
                "to keep playing videos from being marked idle."
            )
        except Exception:
            logger.exception("Windows media detection stopped; normal idle tracking continues.")

    async def _watch(self) -> None:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager,
            GlobalSystemMediaTransportControlsSessionPlaybackStatus,
        )

        manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
        playing_status = GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING

        while not self._stop.is_set():
            playing_apps = set()

            try:
                # Only a session Windows marks as PLAYING should prevent idle time.
                for session in manager.get_sessions() or []:
                    playback = session.get_playback_info()
                    if playback and playback.playback_status == playing_status:
                        app_id = session.source_app_user_model_id.casefold().removesuffix(".exe")
                        if app_id:
                            playing_apps.add(app_id)

                with self._lock:
                    self._playing_apps = playing_apps
                    self._playing_until = time.monotonic() + 3 * self.check_every
            except Exception as error:
                logger.debug("Could not read media sessions: %s", error)

            await asyncio.sleep(self.check_every)


def get_current_activity(media: MediaPlaybackMonitor | None = None) -> Activity | None:
    window = get_active_window()
    if window is None:
        return None

    process_name, window_title = window
    media_is_playing = media is not None and media.is_playing_for(process_name)
    # Watching media counts as activity, but only if the app is actually playing it.
    idle = get_idle_seconds() >= IDLE_AFTER and not media_is_playing

    if process_name.casefold() in BROWSERS:
        title, domain = get_browser_title(process_name, window_title)
        return Activity(BROWSERS[process_name.casefold()][0], title, domain, idle)

    app = process_name.removesuffix(".exe")
    return Activity(app, window_title, "", idle)
