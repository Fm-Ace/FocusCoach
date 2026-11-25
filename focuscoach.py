"""FocusCoach: quietly logs the app and window you're using."""

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
    MediaPlaybackMonitor,
    another_copy_is_running,
    close_instance_handles,
    get_current_activity,
    stop_requested,
)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "focuscoach.db"
LOG_PATH = BASE_DIR / "focuscoach.log"
CHECK_INTERVAL = 0.5

logger = logging.getLogger("focuscoach")


def setup_logging() -> None:
    """Show important warnings and keep a small log file for troubleshooting."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(console)

    # Keep the log small instead of letting it grow forever.
    file = RotatingFileHandler(
        LOG_PATH,
        maxBytes=1_000_000,
        backupCount=2,
        encoding="utf-8",
    )
    file.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(file)


def stop_on_signal(_signum, _frame) -> None:
    """Use the normal cleanup path when Windows asks the program to stop."""
    raise KeyboardInterrupt


def read_activity(media: MediaPlaybackMonitor) -> Activity | None:
    """Read the current activity without letting a Windows hiccup stop tracking."""
    try:
        return get_current_activity(media)
    # Windows can briefly refuse access while windows or processes are changing.
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("Could not read the active window")
        return None


def main() -> int:
    """Start FocusCoach; launching it again requests a graceful stop."""
    # Some Windows terminals use an encoding that cannot print every page title.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    try:
        already_running = another_copy_is_running()
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1

    if already_running:
        print("Stop requested. FocusCoach is shutting down.")
        close_instance_handles()
        return 0

    setup_logging()
    print("Starting FocusCoach...")

    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop_on_signal)

    try:
        log = ActivityLog(DB_PATH)
    except sqlite3.Error as error:
        print(f"Could not open the database: {error}")
        close_instance_handles()
        return 1

    media = MediaPlaybackMonitor()

    try:
        # Keep media checks separate so they never slow down activity tracking.
        media.start()
        print("FocusCoach is running.")
        print(f"A change is saved after {SETTLE_SECONDS} seconds.")
        print("Playing media stays active while Windows reports it as playing.")
        print(f"Database: {DB_PATH}")
        print("Run FocusCoach again or press Ctrl+C to stop.\n")

        while not stop_requested():
            # Check twice per second; ActivityLog filters out very short app switches.
            started = log.record(read_activity(media))
            if started:
                print(started)
            time.sleep(CHECK_INTERVAL)

        print("\nFocusCoach stopped.")

    except KeyboardInterrupt:
        print("\nFocusCoach stopped.")

    finally:
        # Close the database, background thread, and named Windows handles.
        log.close()
        media.stop()
        close_instance_handles()

    return 0


if __name__ == "__main__":
    sys.exit(main())
