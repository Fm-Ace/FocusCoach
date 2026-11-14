"""FocusCoach: logs which app, window title and website you are using.

Run this file. The pieces it uses:
    tracker.py       looks at the computer ("what is the user doing now?")
    activity_log.py  decides when something really changed and saves it
"""

from __future__ import annotations

import logging
import signal
import sqlite3
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from activity_log import SETTLE_SECONDS, Activity, ActivityLog
from tracker import (
    another_copy_is_running,
    get_current_activity,
    start_receiver,
    stop_receiver,
)

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "focuscoach.db"
LOG_PATH = BASE_DIR / "focuscoach.log"
CHECK_INTERVAL = 0.5  # Seconds between looks at the active window.

logger = logging.getLogger("focuscoach")


def setup_logging() -> None:
    """Warnings and errors go to the console; everything goes to a log file."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(console)

    # Keeps at most about 3 MB of old logs, then overwrites the oldest.
    file = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(file)


def stop_on_signal(signum, frame) -> None:
    """Turn "close"/"terminate" signals into Ctrl+C so cleanup always runs."""
    raise KeyboardInterrupt


def read_activity() -> Activity | None:
    """One look at the computer. A failure means "unknown", never a crash."""
    try:
        return get_current_activity()
    except Exception:
        logger.exception("Could not read the active window")
        return None


def start_optional_receiver():
    """The browser tab receiver is optional: without it we use window titles."""
    try:
        return start_receiver()
    except OSError as error:
        print(f"Browser tab receiver is off ({error}). Using window titles only.")
        return None, None


def main() -> int:
    # Window titles can be in any language. Never crash just by printing one.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    setup_logging()
    print("Starting FocusCoach...")

    if another_copy_is_running():
        print("FocusCoach is already running. Close the other copy first.")
        return 1

    for name in ("SIGTERM", "SIGBREAK"):  # SIGBREAK exists on Windows only.
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop_on_signal)

    try:
        log = ActivityLog(DB_PATH)
    except sqlite3.Error as error:
        print(f"Could not open the database: {error}")
        return 1

    server = thread = None

    # Everything after the database opens is inside this try, so Ctrl+C at
    # any moment (even during startup) still closes the open row cleanly.
    try:
        server, thread = start_optional_receiver()

        print("FocusCoach is running.")
        print("Switch between applications to test the tracker.")
        print(f"A change shows up after {SETTLE_SECONDS} seconds; quick glances are ignored.")
        if server:
            host, port = server.server_address
            print(f"Receiver: http://{host}:{port}")
        print(f"Database: {DB_PATH}")
        print("Press Ctrl+C to stop.\n")

        while True:
            started = log.record(read_activity())

            if started:
                print(started)

            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        print("\nFocusCoach stopped.")

    finally:
        log.close()

        if server:
            stop_receiver(server, thread)

    return 0


if __name__ == "__main__":
    sys.exit(main())
