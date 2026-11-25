import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from activity_log import Activity, ActivityLog


class ActivityLogTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "activity.db"
        self.editor = Activity("Editor", "main.py")
        self.browser = Activity("Chrome", "Docs")

    def test_gap_close_retries_without_losing_the_boundary(self):
        log = ActivityLog(self.path)
        self.addCleanup(log.close)
        log.record(self.editor, 100)
        log.record(self.editor, 103)
        log.record(self.editor, 104)
        log.db.execute("""
            CREATE TEMP TRIGGER fail_close BEFORE UPDATE OF end_time ON activity_log
            BEGIN SELECT RAISE(ABORT, 'write failed'); END
        """)

        with self.assertLogs("activity_log", level="ERROR"):
            log.record(self.browser, 120)
        self.assertIsNone(log.record(self.browser, 123))
        self.assertEqual(self.rows(), [(100, None, 100)])

        log.db.execute("DROP TRIGGER fail_close")
        self.assertEqual(log.record(self.browser, 124), self.browser)
        self.assertEqual(self.rows(), [(100, 104, 100), (120, None, 120)])

    def test_shutdown_retries_close_at_gap_boundary(self):
        log = ActivityLog(self.path)
        try:
            log.record(self.editor, 100)
            log.record(self.editor, 103)
            log.db.execute("""
                CREATE TEMP TRIGGER fail_close BEFORE UPDATE OF end_time ON activity_log
                BEGIN SELECT RAISE(ABORT, 'write failed'); END
            """)
            with self.assertLogs("activity_log", level="ERROR"):
                log.record(self.editor, 120)
            log.db.execute("DROP TRIGGER fail_close")
        finally:
            log.close()

        self.assertEqual(self.rows(), [(100, 103, 100)])

    def test_settling_brief_switch_normal_gap_and_recovery(self):
        log = ActivityLog(self.path)
        with closing(log.db):
            self.assertIsNone(log.record(self.editor, 100))
            self.assertEqual(log.record(self.editor, 103), self.editor)
            self.assertIsNone(log.record(self.browser, 104))
            self.assertIsNone(log.record(self.editor, 105))
            self.assertIsNone(log.record(self.browser, 106))
            self.assertEqual(log.record(self.browser, 109), self.browser)
            self.assertIsNone(log.record(self.browser, 120))
            self.assertEqual(log.record(self.browser, 123), self.browser)
            self.assertEqual(self.rows(), [(100, 106, 100), (106, 109, 106), (120, None, 120)])

        recovered = ActivityLog(self.path)
        self.addCleanup(recovered.close)
        self.assertEqual(self.rows()[-1], (120, 120, 120))

    def rows(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(
                "SELECT start_time, end_time, last_seen FROM activity_log ORDER BY id"
            ).fetchall()
