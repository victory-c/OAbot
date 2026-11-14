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
                                -- | duplicate | replaced | dismissed
        kind TEXT, company TEXT, role TEXT, deadline TEXT, link TEXT,
        info_json TEXT,         -- full classifier output
        event_title TEXT, event_start TEXT, event_end TEXT, event_link TEXT,
        reasoning TEXT,         -- why the scheduler picked that slot
        draft_reply TEXT,       -- availability reply draft (never auto-sent)
        prep_md TEXT,           -- interview prep doc (markdown)
        error TEXT
    );
    CREATE TABLE IF NOT EXISTS seen (gmail_id TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    """
)

# Columns added after the first version. Added in place so existing data/oabot.db files keep working.
_NEW_COLUMNS = {
    "event_id": "TEXT",          # calendar event id, so the block can be deleted or moved
    "duplicate_of": "INTEGER",   # for reminders: the item that already covers this
    "followup": "TEXT",          # OAs after the block: asked | done | skipped (null: not asked yet)
    "risk_alerted": "TEXT",      # the deadline we last sent a "due soon, not done" ping for
    "prep_event_id": "TEXT",     # OAs: the warm-up block right before the OA block
    "prep_start": "TEXT",        # OAs: when that warm-up starts (it ends when the OA block starts)
}
_have = {r["name"] for r in _conn.execute("PRAGMA table_info(items)")}
for _col, _type in _NEW_COLUMNS.items():
    if _col not in _have:
        _conn.execute(f"ALTER TABLE items ADD COLUMN {_col} {_type}")
_conn.commit()


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


def items_of_kind(kind: str, statuses: tuple[str, ...], exclude_id: int | None = None) -> list[dict]:
    """Items of one kind in any of the given statuses, oldest first (for duplicate checks)."""
    marks = ",".join("?" * len(statuses))
    with _lock:
        rows = _conn.execute(
            f"SELECT * FROM items WHERE kind=? AND status IN ({marks}) AND id IS NOT ? ORDER BY id",
            (kind, *statuses, exclude_id),
        ).fetchall()
    return [dict(r) for r in rows]


def is_seen(gmail_id: str) -> bool:
    with _lock:
        return _conn.execute("SELECT 1 FROM seen WHERE gmail_id=?", (gmail_id,)).fetchone() is not None


def mark_seen(gmail_id: str):
    with _lock:
        _conn.execute("INSERT OR IGNORE INTO seen (gmail_id) VALUES (?)", (gmail_id,))
        _conn.commit()


def get_meta(key: str):
    with _lock:
        row = _conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(key: str, value: str):
    with _lock:
        _conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        _conn.commit()


def duplicates_of(item_id: int) -> list[dict]:
    """Reminder items that point at this one."""
    with _lock:
        rows = _conn.execute("SELECT * FROM items WHERE duplicate_of=? AND status='duplicate'", (item_id,)).fetchall()
    return [dict(r) for r in rows]


def reset():
    """Forget every item and the mock calendar (before a demo). Real Google events are left alone."""
    with _lock:
        _conn.execute("DELETE FROM items")
        _conn.commit()
    (config.DATA / "mock_calendar.json").unlink(missing_ok=True)


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["reset"]:
        reset()
        print("Cleared all items and the mock calendar. Restart the server.")
    else:
        print("Usage: python -m app.db reset   (clears the dashboard before a demo)")
