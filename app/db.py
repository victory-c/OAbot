"""Tiny SQLite store: one row per recruiting email we acted on."""
import json
import sqlite3
import threading
from datetime import datetime

from . import config

_lock = threading.Lock()
_conn = sqlite3.connect(config.DATA / "oabot.db", check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created TEXT, source TEXT, sender TEXT, subject TEXT, body TEXT,
        status TEXT,            -- processing | scheduled | prepped | needs_reply | ignored | error
        kind TEXT, company TEXT, role TEXT, deadline TEXT, link TEXT,
        info_json TEXT,         -- full classifier output
        event_title TEXT, event_start TEXT, event_end TEXT, event_link TEXT,
        reasoning TEXT,         -- why the scheduler picked that slot
        draft_reply TEXT,       -- availability reply draft (never auto-sent)
        prep_md TEXT,           -- interview prep doc (markdown)
        error TEXT
    );
    CREATE TABLE IF NOT EXISTS seen (gmail_id TEXT PRIMARY KEY);
    """
)


def add_item(source, sender, subject, body) -> int:
    with _lock:
        cur = _conn.execute(
            "INSERT INTO items (created, source, sender, subject, body, status) VALUES (?,?,?,?,?,?)",
            (datetime.now(config.TZ).isoformat(timespec="seconds"), source, sender, subject, body, "processing"),
        )
        _conn.commit()
        return cur.lastrowid


def update_item(item_id: int, **fields):
    if not fields:
        return
    if "info_json" in fields and not isinstance(fields["info_json"], str):
        fields["info_json"] = json.dumps(fields["info_json"])
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        _conn.execute(f"UPDATE items SET {cols} WHERE id=?", (*fields.values(), item_id))
        _conn.commit()


def get_item(item_id: int):
    with _lock:
        row = _conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return dict(row) if row else None


def list_items():
    with _lock:
        rows = _conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def is_seen(gmail_id: str) -> bool:
    with _lock:
        return _conn.execute("SELECT 1 FROM seen WHERE gmail_id=?", (gmail_id,)).fetchone() is not None


def mark_seen(gmail_id: str):
    with _lock:
        _conn.execute("INSERT OR IGNORE INTO seen (gmail_id) VALUES (?)", (gmail_id,))
        _conn.commit()
