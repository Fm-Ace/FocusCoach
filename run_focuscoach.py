"""Launch FocusCoach as a background process and toggle it with repeated runs."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pywintypes
import win32con
import win32event
import winerror

from tracker import STOP_EVENT_NAME

BASE_DIR = Path(__file__).resolve().parent
FOCUSCOACH_PATH = BASE_DIR / "focuscoach.py"
LOG_PATH = BASE_DIR / "focuscoach.log"
STARTUP_TIMEOUT = 5.0
POLL_INTERVAL = 0.05


def _open_stop_event(access: int):
    """Open the tracker event, returning None when no tracker has created it."""
    try:
        return win32event.OpenEvent(access, False, STOP_EVENT_NAME)
    except pywintypes.error as error:
        error_code = getattr(error, "winerror", None)
        if error_code is None and error.args:
            error_code = error.args[0]
        if error_code == winerror.ERROR_FILE_NOT_FOUND:
            return None
        raise


def request_stop() -> bool:
    """Ask an existing tracker to stop; return False when none is running."""
    event = _open_stop_event(win32con.EVENT_MODIFY_STATE)
    if event is None:
        return False

    try:
        win32event.SetEvent(event)
    finally:
        event.Close()

    return True


def wait_for_start() -> bool:
    """Wait until the background tracker has created its stop event."""
    deadline = time.monotonic() + STARTUP_TIMEOUT

    while time.monotonic() < deadline:
        event = _open_stop_event(win32con.SYNCHRONIZE)
        if event is not None:
            event.Close()
            return True
        time.sleep(POLL_INTERVAL)

    return False


def main() -> int:
    """Start the tracker, or request a graceful stop when it is already running."""
    try:
        if request_stop():
            print("Stop requested. FocusCoach is shutting down.")
            return 0

        subprocess.Popen(
            [sys.executable, str(FOCUSCOACH_PATH)],
            cwd=BASE_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, pywintypes.error) as error:
        print(f"Could not start or stop FocusCoach: {error}", file=sys.stderr)
        return 1

    if wait_for_start():
        print("FocusCoach started. Run Code again to stop it.")
        return 0

    print(
        f"FocusCoach did not report that it started. Check {LOG_PATH}.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
