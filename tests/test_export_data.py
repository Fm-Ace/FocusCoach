import csv
import hashlib
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from activity_log import ActivityLog
from ml import export_data


class ExportDataTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.database = self.root / "activity # é.db"
        self.output = self.root / "new" / "data" / "activities.csv"
        ActivityLog(self.database).close()

    def insert(self, rows):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executemany(
                "INSERT INTO activity_log (id, app, title, domain, idle, start_time, end_time) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def read_csv(self):
        with self.output.open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))

    def digest(self):
        return hashlib.sha256(self.database.read_bytes()).digest()

    def test_success_duration_order_incomplete_invalid_and_directory(self):
        self.insert([
            (1, "Browser", "Later, title", "", 0, 110, 120.5),
            (2, "Editor", 'Earlier "title"', "", 1, 100, 105),
            (3, "App", "Open", "", 0, 130, None),
            (4, "App", "Backwards", "", 0, 140, 139),
            (5, "App", "Idle invalid", "", 2, 150, 151),
            (6, "App", "Time invalid", "", 0, "bad", 160),
            (7, "App", "Infinite", "", 0, 160, float("inf")),
            (8, sqlite3.Binary(b"bad"), "Binary app", "", 0, 170, 180),
            (9, "App", "Zero duration", "", 0, 190, 190),
        ])
        before = self.digest()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = export_data.main(["--db", str(self.database), "--output", str(self.output)])

        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue(), "Exported 3 records; excluded 1 incomplete and 5 invalid records.\n")
        rows = self.read_csv()
        self.assertEqual(list(rows[0]), [
            "id", "app", "title", "domain", "idle", "start_time", "end_time", "duration_seconds"
        ])
        self.assertEqual([row["id"] for row in rows], ["2", "1", "9"])
        self.assertEqual([float(row["duration_seconds"]) for row in rows], [5, 10.5, 0])
        self.assertEqual(rows[0]["title"], 'Earlier "title"')
        self.assertEqual(rows[1]["title"], "Later, title")
        self.assertEqual(self.digest(), before)

    def test_script_from_another_working_directory(self):
        self.insert([(1, "Editor", "Synthetic", "", 0, 10, 13.5)])
        before = self.digest()
        script = Path(export_data.__file__).resolve()
        result = subprocess.run(
            [sys.executable, str(script), "--db", str(self.database), "--output", str(self.output)],
            cwd=self.root, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Exported 1 records; excluded 0 incomplete and 0 invalid records.\n")
        self.assertEqual(float(self.read_csv()[0]["duration_seconds"]), 3.5)
        self.assertEqual(self.digest(), before)

    def test_default_paths_from_another_working_directory(self):
        self.insert([(1, "Editor", "Synthetic", "", 0, 10, 12)])
        project = self.root / "project"
        script = project / "ml" / "export_data.py"
        script.parent.mkdir(parents=True)
        shutil.copyfile(export_data.__file__, script)
        default_database = project / "focuscoach.db"
        shutil.copyfile(self.database, default_database)
        before = hashlib.sha256(default_database.read_bytes()).digest()
        result = subprocess.run([sys.executable, str(script)], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Exported 1 records; excluded 0 incomplete and 0 invalid records.\n")
        with (project / "ml" / "data" / "activities.csv").open(encoding="utf-8", newline="") as stream:
            self.assertEqual(float(next(csv.DictReader(stream))["duration_seconds"]), 2)
        self.assertEqual(hashlib.sha256(default_database.read_bytes()).digest(), before)

    def test_connection_is_read_only(self):
        self.insert([(1, "App", "Title", "", 0, 1, 2)])
        connect = sqlite3.connect
        before = self.digest()

        def check_connection(*args, **kwargs):
            self.assertTrue(kwargs["uri"])
            connection = connect(*args, **kwargs)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM activity_log")
            connection.rollback()
            return connection

        with patch.object(export_data.sqlite3, "connect", side_effect=check_connection):
            self.assertEqual(export_data.export_data(self.database, self.output), (1, 0, 0))
        self.assertEqual(self.digest(), before)

    def test_invalid_utf8_row_is_excluded(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "INSERT INTO activity_log (id, app, title, domain, idle, start_time, end_time) "
                "VALUES (1, 'App', CAST(x'80' AS TEXT), '', 0, 1, 2)"
            )
        self.insert([(2, "App", "Valid", "", 0, 3, 4)])
        before = self.digest()

        self.assertEqual(export_data.export_data(self.database, self.output), (1, 0, 1))
        self.assertEqual([row["title"] for row in self.read_csv()], ["Valid"])
        self.assertEqual(self.digest(), before)

    def test_missing_database_does_not_create_database_or_output(self):
        missing = self.root / "missing.db"
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(export_data.main(["--db", str(missing), "--output", str(self.output)]), 1)
        self.assertIn("Export failed:", stderr.getvalue())
        self.assertFalse(missing.exists())
        self.assertFalse(self.output.exists())

    def test_bad_database_or_schema_preserves_existing_output(self):
        self.output.parent.mkdir(parents=True)
        self.output.write_text("original", encoding="utf-8")
        invalid = self.root / "invalid.db"
        invalid.write_bytes(b"not a database")
        wrong_schema = self.root / "wrong.db"
        with closing(sqlite3.connect(wrong_schema)) as db, db:
            db.execute("CREATE TABLE activity_log (id INTEGER PRIMARY KEY)")
        wrong_types = self.root / "wrong-types.db"
        with closing(sqlite3.connect(wrong_types)) as db, db:
            db.execute(
                "CREATE TABLE activity_log (id TEXT, app TEXT, title TEXT, domain TEXT, "
                "idle INTEGER, start_time REAL, end_time REAL)"
            )
        view = self.root / "view.db"
        with closing(sqlite3.connect(view)) as db, db:
            db.execute(
                "CREATE VIEW activity_log AS SELECT 1 AS id, 'app' AS app, 'title' AS title, "
                "'' AS domain, 0 AS idle, 1 AS start_time, 2 AS end_time"
            )
        for source in (invalid, wrong_schema, wrong_types, view):
            with self.subTest(source=source.name):
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(export_data.main(["--db", str(source), "--output", str(self.output)]), 1)
                self.assertEqual(self.output.read_text(encoding="utf-8"), "original")

    def test_output_cannot_target_database_or_hardlink(self):
        self.insert([(1, "App", "Title", "", 0, 1, 2)])
        before = self.digest()
        alias = self.root / "alias"
        os.link(self.database, alias)
        for target in (self.database, alias):
            with self.subTest(target=target):
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(export_data.main(["--db", str(self.database), "--output", str(target)]), 1)
                self.assertEqual(self.digest(), before)

    def test_sqlite_sidecar_paths_are_protected(self):
        before = self.digest()
        for suffix in ("-journal", "-wal", "-shm"):
            target = Path(str(self.database) + suffix)
            with self.subTest(suffix=suffix), redirect_stderr(io.StringIO()):
                self.assertEqual(export_data.main(["--db", str(self.database), "--output", str(target)]), 1)
                self.assertFalse(target.exists())
        self.assertEqual(self.digest(), before)

    def test_output_failure_keeps_database_and_existing_output(self):
        self.insert([(1, "App", "Title", "", 0, 1, 2)])
        self.output.parent.mkdir(parents=True)
        self.output.write_text("original", encoding="utf-8")
        before = self.digest()
        with patch.object(export_data.os, "replace", side_effect=OSError("write failed")):
            with redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(export_data.main(["--db", str(self.database), "--output", str(self.output)]), 1)
        self.assertIn("write failed", stderr.getvalue())
        self.assertEqual(self.output.read_text(encoding="utf-8"), "original")
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])
        self.assertEqual(self.digest(), before)

    def test_empty_table_exports_header(self):
        self.assertEqual(export_data.export_data(self.database, self.output), (0, 0, 0))
        with self.output.open(encoding="utf-8", newline="") as stream:
            self.assertEqual(next(csv.reader(stream)), [*export_data.COLUMNS, "duration_seconds"])
        self.assertEqual(self.read_csv(), [])


if __name__ == "__main__":
    unittest.main()
