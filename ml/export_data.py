"""Export completed activity records to a local CSV file."""

import argparse
import csv
import math
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = PROJECT_DIR / "focuscoach.db"
DEFAULT_OUTPUT = PROJECT_DIR / "ml" / "data" / "activities.csv"
COLUMNS = ("id", "app", "title", "domain", "idle", "start_time", "end_time")


def decode_text(value):
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return value


def column_affinity(declared_type):
    declared_type = declared_type.upper()
    if "INT" in declared_type:
        return "INTEGER"
    if any(part in declared_type for part in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if not declared_type or "BLOB" in declared_type:
        return "BLOB"
    if any(part in declared_type for part in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def valid_record(row):
    row_id, app, title, domain, idle, start, end = row
    if not isinstance(row_id, int) or not all(isinstance(value, str) for value in (app, title, domain)):
        return False
    if not isinstance(idle, int) or idle not in (0, 1):
        return False
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in (start, end)):
        return False
    return end >= start and math.isfinite(end - start)


def export_data(database, output):
    database = Path(database).resolve(strict=True)
    output = Path(output)
    output_resolved = output.resolve()
    for protected in (database, *(Path(str(database) + suffix) for suffix in ("-journal", "-wal", "-shm"))):
        if output_resolved == protected or (output.exists() and protected.exists() and os.path.samefile(output, protected)):
            raise ValueError("Output must not overwrite the database or its SQLite files")

    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.text_factory = decode_text
        connection.execute("BEGIN")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Database integrity check failed")
        table = connection.execute(
            "SELECT type FROM sqlite_master WHERE name = 'activity_log'"
        ).fetchone()
        columns = {
            row[1]: column_affinity(row[2])
            for row in connection.execute("PRAGMA table_info(activity_log)")
        }
        expected = {
            "id": {"INTEGER"}, "app": {"TEXT"}, "title": {"TEXT"},
            "domain": {"TEXT"}, "idle": {"INTEGER"},
            "start_time": {"REAL", "INTEGER", "NUMERIC"},
            "end_time": {"REAL", "INTEGER", "NUMERIC"},
        }
        if table != ("table",) or any(columns.get(name) not in types for name, types in expected.items()):
            raise ValueError("Incompatible activity_log schema")

        rows = connection.execute(
            "SELECT id, app, title, domain, idle, start_time, end_time "
            "FROM activity_log ORDER BY start_time, id"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", dir=output.parent, delete=False
            ) as temp:
                temp_path = Path(temp.name)
                writer = csv.writer(temp)
                writer.writerow((*COLUMNS, "duration_seconds"))
                exported = incomplete = invalid = 0
                for row in rows:
                    if row[-1] is None:
                        incomplete += 1
                    elif not valid_record(row):
                        invalid += 1
                    else:
                        writer.writerow((*row, row[-1] - row[-2]))
                        exported += 1
            os.replace(temp_path, output)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    return exported, incomplete, invalid


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE, help="SQLite database path")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="CSV output path")
    args = parser.parse_args(argv)

    try:
        exported, incomplete, invalid = export_data(args.db, args.output)
    except (OSError, sqlite3.Error, ValueError, UnicodeError) as error:
        print(f"Export failed: {error}", file=sys.stderr)
        return 1

    print(f"Exported {exported} records; excluded {incomplete} incomplete and {invalid} invalid records.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
