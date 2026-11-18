"""Tiny data layer.

One SQL dialect-neutral surface: write SQL with `?` placeholders and the shim
rewrites them to `%s` for Postgres. Postgres is used when DATABASE_URL points at
one (Render); otherwise SQLite keeps local dev and tests fast.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Sequence
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
SQLITE_PATH = os.environ.get(
    "SQLITE_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "pantry.db")
)

Row = dict[str, Any]


def backend() -> str:
    """Which engine is live — logged at startup so deploy logs say it plainly."""
    return "postgres" if IS_PG else "sqlite"


def _pg_dsn() -> str:
    """psycopg rejects the postgres:// scheme Render hands out verbatim."""
    if DATABASE_URL.startswith("postgres://"):
        return "postgresql://" + DATABASE_URL[len("postgres://") :]
    return DATABASE_URL


def _pg_conn() -> psycopg.Connection[Any]:
    # dict_row is correct at runtime; psycopg's stub types it for TupleRow only.
    return psycopg.connect(_pg_dsn(), row_factory=cast(Any, dict_row), autocommit=False)


def _sqlite_conn() -> sqlite3.Connection:
    path = os.path.abspath(SQLITE_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _sql(query: str) -> str:
    return query.replace("?", "%s") if IS_PG else query


def query(sql: str, params: Sequence[Any] = ()) -> list[Row]:
    if IS_PG:
        with _pg_conn() as conn:
            cur = conn.cursor()
            # psycopg's stub only accepts its LiteralString template type here.
            cur.execute(cast(Any, _sql(sql)), tuple(params))
            rows = cast(list[Row], cur.fetchall())
            cur.close()
            return rows
    conn = _sqlite_conn()
    try:
        return [dict(r) for r in conn.execute(_sql(sql), tuple(params)).fetchall()]
    finally:
        conn.close()


def execute(sql: str, params: Sequence[Any] = ()) -> Row | None:
    """Run a write statement. Returns the first row if the statement RETURNs."""
    if IS_PG:
        with _pg_conn() as conn:
            cur = conn.cursor()
            cur.execute(cast(Any, _sql(sql)), tuple(params))
            row = cast(Row | None, cur.fetchone()) if cur.description is not None else None
            cur.close()
            conn.commit()
            return row
    conn = _sqlite_conn()
    try:
        cur = conn.execute(_sql(sql), tuple(params))
        row = dict(cur.fetchone()) if cur.description is not None else None
        conn.commit()
        return row
    finally:
        conn.close()



SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS containers (
    id SERIAL PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS items (
    id SERIAL PRIMARY KEY,
    token TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    size TEXT NOT NULL DEFAULT '',
    quantity INTEGER NOT NULL DEFAULT 1,
    added_on TEXT NOT NULL DEFAULT '',
    best_before TEXT NOT NULL DEFAULT '',
    container_id INTEGER REFERENCES containers(id) ON DELETE SET NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS withdrawals (
    id SERIAL PRIMARY KEY,
    item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
    container_code TEXT NOT NULL DEFAULT '',
    item_name TEXT NOT NULL DEFAULT '',
    quantity INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT '',
    withdrawn_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_items_container ON items(container_id);
CREATE INDEX IF NOT EXISTS idx_withdrawals_item ON withdrawals(item_id);
"""

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS containers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    size TEXT NOT NULL DEFAULT '',
    quantity INTEGER NOT NULL DEFAULT 1,
    added_on TEXT NOT NULL DEFAULT '',
    best_before TEXT NOT NULL DEFAULT '',
    container_id INTEGER REFERENCES containers(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
    container_code TEXT NOT NULL DEFAULT '',
    item_name TEXT NOT NULL DEFAULT '',
    quantity INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT '',
    withdrawn_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_items_container ON items(container_id);
CREATE INDEX IF NOT EXISTS idx_withdrawals_item ON withdrawals(item_id);
"""


def init_db() -> None:
    if IS_PG:
        with _pg_conn() as conn:
            cur = conn.cursor()
            cur.execute(SCHEMA_PG)
            cur.close()
            conn.commit()
        return
    conn = _sqlite_conn()
    try:
        conn.executescript(SCHEMA_SQLITE)
        conn.commit()
    finally:
        conn.close()


# Item rows always carry the derived "withdrawn" total so pages can show what is left.
ITEM_SELECT = """
SELECT i.id, i.token, i.name, i.description, i.size, i.quantity, i.added_on,
       i.best_before, i.container_id, i.created_at,
       COALESCE((SELECT SUM(w.quantity) FROM withdrawals w WHERE w.item_id = i.id), 0) AS withdrawn,
       c.code AS container_code, c.name AS container_name
FROM items i
LEFT JOIN containers c ON c.id = i.container_id
"""


def decorate_items(rows: list[Row]) -> list[Row]:
    for r in rows:
        r["remaining"] = max(int(r["quantity"]) - int(r["withdrawn"] or 0), 0)
        r["in_stock"] = r["remaining"] > 0
    return rows


def get_items(
    where: str = "", params: Sequence[Any] = (), order: str = "ORDER BY i.name"
) -> list[Row]:
    return decorate_items(query(f"{ITEM_SELECT} {where} {order}", params))
