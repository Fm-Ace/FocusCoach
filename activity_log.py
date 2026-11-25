"""Saves what the user was doing to a SQLite file, one row per activity.

This file knows nothing about Windows. It is fed the current activity again
and again (twice a second) through ActivityLog.record() and decides when
something has really changed.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# A change must last this long before it is logged, so quick glances at
# another window don't create rows.
SETTLE_SECONDS = 3

# If record() isn't called for this long, the computer slept or the program
# froze. We can't know what happened in between, so the open row is closed.
GAP_SECONDS = 10

# How often the open row's "last_seen" time is refreshed. After a crash, the
# row is closed at that time the next time the program starts.
HEARTBEAT_SECONDS = 10

# The same database error is only written to the log this often (seconds).
ERROR_REPEAT_SECONDS = 30

SCHEMA_VERSION = 1

NUMBERS = re.compile(r"\d+")


@dataclass(frozen=True)
class Activity:
    """One thing the user can be doing: an app, a window title, maybe a site."""

    app: str
    title: str
    domain: str = ""
    idle: bool = False

    @property
    def signature(self) -> tuple:
        """What makes two activities 'the same'.

        Numbers and save markers in titles are ignored, so "(3) Inbox" and
        "(4) Inbox", or "main.py" and "*main.py", don't create new rows.
        """
        title = NUMBERS.sub("#", self.title).strip("*● ")
        return (self.app, title, self.domain, self.idle)

    def __str__(self) -> str:
        text = f"[{self.app}] {self.title}"

        if self.domain:
            text += f" — {self.domain}"

        if self.idle:
            text += " (idle)"

        return text


def same_activity(first: Activity | None, second: Activity | None) -> bool:
    """True if both are the same activity. None means "unknown window"."""
    if first is None or second is None:
        return first is second

    return first.signature == second.signature


@dataclass
class _Pending:
    """A change we have seen but haven't trusted yet."""

    activity: Activity | None
    since: float


class ActivityLog:
    """Turns a stream of "what is the user doing right now?" into rows.

    Only one copy of the program may use the database at a time (see
    another_copy_is_running in tracker.py). Opening it closes any row that a
    crashed run left open.
    """

    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(path, timeout=5)

        self.logged: Activity | None = None  # What the open row says.
        self.row_id: int | None = None  # Id of the open row, if any.
        self.pending: _Pending | None = None
        self.last_call: float | None = None
        self._gap_end: float | None = None
        self.last_heartbeat = 0.0
        self._last_error = ("", 0.0)

        try:
            self._prepare_database()
        except sqlite3.Error:
            self.db.close()
            raise

    def record(self, activity: Activity | None, now: float | None = None) -> Activity | None:
        """Tell the log what the user is doing right now.

        Returns the activity when a new row was just opened, otherwise None.

        - A change only counts once it has lasted SETTLE_SECONDS. The new row
          is dated from when the change first appeared.
        - Going back to the logged activity before that cancels the change,
          so a quick glance elsewhere never splits the row.
        - If record() wasn't called for GAP_SECONDS (sleep, freeze), the open
          row is closed at the last time we knew the user was there.
        """
        if now is None:
            now = time.time()

        self._close_after_gap(now)
        self.last_call = now
        if self._gap_end is None:
            self._heartbeat(now)

        logged = self.logged if self._gap_end is None else None
        if same_activity(activity, logged):
            self.pending = None
            return None

        if self.pending is None or not same_activity(activity, self.pending.activity):
            self.pending = _Pending(activity, since=now)

        if self._gap_end is not None or now - self.pending.since < SETTLE_SECONDS:
            return None

        if self._switch_to(activity, at=self.pending.since):
            return activity

        return None

    def close(self) -> None:
        """Close the open row and the database."""
        self._close_row(at=self._gap_end if self._gap_end is not None else time.time())
        self.db.close()

    def _prepare_database(self) -> None:
        with self.db:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]

            if version < SCHEMA_VERSION:
                self._create_or_upgrade_table()

            # A crash leaves the last row without an end time. Close it where
            # the heartbeat last saw it; old rows without one get zero length.
            self.db.execute(
                """
                UPDATE activity_log
                SET end_time = COALESCE(last_seen, start_time)
                WHERE end_time IS NULL
                """
            )

    def _create_or_upgrade_table(self) -> None:
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_log (
                id         INTEGER PRIMARY KEY,
                start_time REAL    NOT NULL,
                end_time   REAL,
                last_seen  REAL,
                app        TEXT    NOT NULL,
                title      TEXT    NOT NULL,
                domain     TEXT    NOT NULL,
                idle       INTEGER NOT NULL
            )
            """
        )

        # Databases made by older versions don't have the last_seen column.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(activity_log)")}
        if "last_seen" not in columns:
            self.db.execute("ALTER TABLE activity_log ADD COLUMN last_seen REAL")

        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_activity_log_start "
            "ON activity_log (start_time)"
        )
        self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _close_after_gap(self, now: float) -> None:
        if self.last_call is not None and not self.last_call <= now <= self.last_call + GAP_SECONDS:
            self.pending = None
            if self._gap_end is None:
                self._gap_end = self.last_call

        # Preserve the first gap's end time until the write succeeds.
        if self._gap_end is not None and self._close_row(at=self._gap_end):
            self.logged = None
            self.row_id = None
            self._gap_end = None

    def _heartbeat(self, now: float) -> None:
        if self.row_id is None or now - self.last_heartbeat < HEARTBEAT_SECONDS:
            return

        try:
            with self.db:
                self.db.execute(
                    "UPDATE activity_log SET last_seen = ? WHERE id = ?",
                    (now, self.row_id),
                )
            self.last_heartbeat = now
        except sqlite3.Error as error:
            self._report(error)

    def _switch_to(self, activity: Activity | None, at: float) -> bool:
        """Close the open row and open one for `activity`, in one transaction.

        If the database refuses, nothing changes and the next record() call
        tries again, so no time is lost and no row is left half-closed.
        """
        try:
            with self.db:
                if self.row_id is not None:
                    self._end_row(self.row_id, at)

                new_id = None
                if activity is not None:
                    cursor = self.db.execute(
                        """
                        INSERT INTO activity_log
                            (start_time, last_seen, app, title, domain, idle)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            at,
                            at,
                            activity.app,
                            activity.title,
                            activity.domain,
                            int(activity.idle),
                        ),
                    )
                    new_id = cursor.lastrowid
        except sqlite3.Error as error:
            self._report(error)
            return False

        self.row_id = new_id
        self.logged = activity
        self.pending = None
        self.last_heartbeat = at
        return True

    def _close_row(self, at: float) -> bool:
        if self.row_id is None:
            return True

        try:
            with self.db:
                self._end_row(self.row_id, at)
        except sqlite3.Error as error:
            self._report(error)
            return False
        return True

    def _end_row(self, row_id: int, at: float) -> None:
        # MAX() keeps the end from landing before the start if the clock moved.
        self.db.execute(
            "UPDATE activity_log SET end_time = MAX(?, start_time) WHERE id = ?",
            (at, row_id),
        )

    def _report(self, error: sqlite3.Error) -> None:
        """Log a database problem, but not 100 times a minute."""
        message = str(error)
        now = time.monotonic()
        last_message, last_time = self._last_error

        if message != last_message or now - last_time >= ERROR_REPEAT_SECONDS:
            logger.error("Could not save to the database: %s", message)
            self._last_error = (message, now)
