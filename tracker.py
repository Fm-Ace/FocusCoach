"""Read the active Windows app and its window title."""

from __future__ import annotations

import asyncio
import logging
import re
import threading

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
    "msedge.exe": ("edge",),
}

_instance_mutex = None


def another_copy_is_running() -> bool:
    """Keep one tracker connected to the database at a time."""
    global _instance_mutex

    try:
        _instance_mutex = win32event.CreateMutex(None, False, MUTEX_NAME)
        return win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    except pywintypes.error:
        logger.warning("Could not check whether another copy is running.")
        return False


def clean_title(title: str) -> str:
    return " ".join(title.split())[:MAX_TITLE_LENGTH]


def get_idle_seconds() -> float:
    last_input = win32api.GetLastInputInfo()
    current_tick = win32api.GetTickCount()
    return ((current_tick - last_input) & 0xFFFFFFFF) / 1000


def get_process_name(process_id: int) -> str:
    try:
        return psutil.Process(process_id).name()
    except psutil.AccessDenied:
        return PROTECTED_APP


def get_real_uwp_process(window: int, host_process_id: int) -> int:
    """Find the app inside Windows' shared Store-app host process."""
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
    browser = BROWSERS.get(process_name.casefold())
    if not browser:
        return title, ""

    browser_name, suffix = browser
    pattern = rf"\s+[-—–]\s+{re.escape(suffix)}\s*$"
    page_title = re.sub(pattern, "", title, flags=re.IGNORECASE).strip()
    return page_title or title, ""


class MediaPlaybackMonitor:
    """Keep track of apps Windows currently reports as playing media."""

    def __init__(self, check_every: float = 1.0):
        self.check_every = check_every
        self._playing_apps: set[str] = set()
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
        app = process_name.casefold()
        aliases = MEDIA_ALIASES.get(app, (app.removesuffix(".exe"),))

        with self._lock:
            return any(
                alias in source
                for alias in aliases
                for source in self._playing_apps
            )

    def _run(self) -> None:
        try:
            asyncio.run(self._watch())
        except ImportError:
            logger.warning(
                "Media detection is unavailable. Install the project requirements "
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
                for session in manager.get_sessions() or []:
                    playback = session.get_playback_info()
                    if playback and playback.playback_status == playing_status:
                        app_id = session.source_app_user_model_id.casefold()
                        if app_id:
                            playing_apps.add(app_id)

                with self._lock:
                    self._playing_apps = playing_apps
            except Exception as error:
                logger.debug("Could not read media sessions: %s", error)

            await asyncio.sleep(self.check_every)


def get_current_activity(media: MediaPlaybackMonitor | None = None) -> Activity | None:
    window = get_active_window()
    if window is None:
        return None

    process_name, window_title = window
    media_is_playing = media is not None and media.is_playing_for(process_name)
    idle = get_idle_seconds() >= IDLE_AFTER and not media_is_playing

    if process_name.casefold() in BROWSERS:
        title, domain = get_browser_title(process_name, window_title)
        return Activity(BROWSERS[process_name.casefold()][0], title, domain, idle)

    app = process_name.removesuffix(".exe")
    return Activity(app, window_title, "", idle)
