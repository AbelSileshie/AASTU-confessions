"""SQLite access: one connection per thread, WAL, migrations, tiny helpers.

All queries are parameterised. Never build SQL from user input.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

_local = threading.local()
_db_path: str | None = None


def init(path: str) -> None:
    """Set the database path and run migrations. Call once at startup."""
    global _db_path
    _db_path = path
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    migrate()


def _connect() -> sqlite3.Connection:
    if _db_path is None:
        raise RuntimeError("db.init() has not been called")
    conn = sqlite3.connect(_db_path, timeout=5.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != _db_path:
        c = _connect()
        _local.conn = c
        _local.path = _db_path
    return c


def reset_for_tests(path: str = ":memory:") -> None:
    """Fresh database for tests. In-memory DBs are per-connection, so a
    file path in a temp dir is preferred when threads are involved."""
    global _db_path
    old = getattr(_local, "conn", None)
    if old is not None:
        old.close()
    _local.conn = None
    _db_path = None
    init(path)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def q(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return conn().execute(sql, tuple(params)).fetchall()


def one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return conn().execute(sql, tuple(params)).fetchone()


def scalar(sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
    row = one(sql, params)
    return row[0] if row is not None else default


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    """Run a write statement; return lastrowid (or rowcount for UPDATE/DELETE)."""
    cur = conn().execute(sql, tuple(params))
    if sql.lstrip()[:6].upper() == "INSERT":
        return cur.lastrowid
    return cur.rowcount


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """Explicit transaction. Nested use is not supported."""
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def now() -> int:
    return int(time.time())


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

def _applied() -> set[str]:
    conn().execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at INT)"
    )
    return {r["name"] for r in q("SELECT name FROM schema_migrations")}


def migrate() -> list[str]:
    """Apply every migrations/NNNN_name.sql not yet applied, in order."""
    applied = _applied()
    files = sorted(p for p in MIGRATIONS_DIR.iterdir() if _MIGRATION_NAME.match(p.name))
    done: list[str] = []
    for path in files:
        if path.name in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        # One script = one transaction, so a crash can never leave a migration applied but unrecorded
        # (which would re-run ALTER TABLE on the next start). path.name is regex-validated: no quotes possible.
        c = conn()
        try:
            c.executescript(  # nosec B608 - name matches ^\d{4}_[a-z0-9_]+\.sql$
                "BEGIN;\n" + sql + "\n"
                f"INSERT INTO schema_migrations(name, applied_at) VALUES ('{path.name}', {now()});\n"
                "COMMIT;"
            )
        except sqlite3.Error:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise
        done.append(path.name)
    return done


def audit(actor: int, action: str, target: str = "", detail: str = "") -> None:
    execute(
        "INSERT INTO audit(actor, action, target, detail, at) VALUES (?,?,?,?,?)",
        (actor, action, target, detail, now()),
    )

