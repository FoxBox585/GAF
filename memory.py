"""One-table SQLite ledger. The model decides what goes in and what comes out."""

import os
import sqlite3

PROMPT_CAP = 6000  # max characters of ledger injected into the system prompt

_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(os.getenv("DB_PATH", "./gaf.db"), check_same_thread=False)
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS memories ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " content TEXT NOT NULL,"
            " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        # One column, because there is exactly one flag. A key/value settings table
        # would only invite a config system.
        _conn.execute("CREATE TABLE IF NOT EXISTS muted (name TEXT PRIMARY KEY)")
        _conn.commit()
    return _conn


def add(content: str) -> int | None:
    """Insert a memory; returns its id, or None if an identical one already exists."""
    db = _db()
    if db.execute("SELECT 1 FROM memories WHERE content = ?", (content,)).fetchone():
        return None
    cur = db.execute("INSERT INTO memories (content) VALUES (?)", (content,))
    db.commit()
    return cur.lastrowid


def delete(mem_id: int) -> bool:
    db = _db()
    cur = db.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
    db.commit()
    return cur.rowcount > 0


def muted_names() -> set[str]:
    return {row[0] for row in _db().execute("SELECT name FROM muted")}


def set_muted(name: str, on: bool) -> None:
    db = _db()
    if on:
        db.execute("INSERT OR IGNORE INTO muted (name) VALUES (?)", (name,))
    else:
        db.execute("DELETE FROM muted WHERE name = ?", (name,))
    db.commit()


def render_all() -> str:
    """The whole ledger, uncapped — for /memory, where truncation would be a lie."""
    rows = _db().execute("SELECT id, content FROM memories ORDER BY id").fetchall()
    return "\n".join(f"#{mem_id} {content}" for mem_id, content in rows)


def render_for_prompt() -> str:
    rows = _db().execute("SELECT id, content FROM memories ORDER BY id").fetchall()
    lines = [f"- [#{mem_id}] {content}" for mem_id, content in rows]
    while lines and sum(len(line) + 1 for line in lines) > PROMPT_CAP:
        lines.pop(0)  # oldest out first
    return "\n".join(lines)
