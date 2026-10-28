"""Database access and versioned migrations for N4MI Calendar Display.

SQLite file, one connection per request. Migrations are the numbered .sql
files in app/migrations/, applied in order and recorded in schema_migrations.
Before applying a migration to a database that already holds data, a
consistent copy is written next to it (sqlite backup API, never a raw copy).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_.+\.sql$")


def connect(db_path: str) -> sqlite3.Connection:
    """Open the database with foreign keys enforced and rows as dicts."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def available_migrations() -> list[tuple[int, Path]]:
    found = []
    for path in MIGRATIONS_DIR.iterdir():
        m = _MIGRATION_RE.match(path.name)
        if m:
            found.append((int(m.group(1)), path))
    return sorted(found)


def schema_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, applied TEXT NOT NULL)"
    )
    row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    return row["v"] or 0


def _backup_before_migration(conn: sqlite3.Connection, db_path: str, version: int) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = Path(f"{db_path}.pre-v{version}-{stamp}.bak")
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def migrate(db_path: str) -> list[int]:
    """Apply any pending migrations. Returns the versions applied (may be empty)."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    applied: list[int] = []
    try:
        current = schema_version(conn)
        pending = [(v, p) for v, p in available_migrations() if v > current]
        if pending and current > 0:
            _backup_before_migration(conn, db_path, current)
        for version, path in pending:
            sql = path.read_text(encoding="utf-8")
            now = datetime.now().isoformat(timespec="seconds")
            # One transaction per migration: all of it, or none of it.
            conn.executescript(
                "BEGIN;\n"
                + sql
                + f"\nINSERT INTO schema_migrations (version, applied) VALUES ({version}, '{now}');\n"
                + "COMMIT;"
            )
            applied.append(version)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return applied
