"""After the block: make sure the OA actually got done.

A background check runs every minute:
1. "Did you take it?" When an OA block has ended, the card asks Done / Reschedule / Skipped
   and a ping goes out once.
2. Deadline at risk. When an OA is due within 24 hours and nothing shows it is done (its
   block could not be made, it was dismissed, or its block passed without a Done), one ping
   goes out with the reason. A new deadline from a later email re-arms it.
"""
import threading
import time
import traceback
from datetime import datetime, timedelta

from . import config, db, gcal, notify, pipeline

RISK_WINDOW = timedelta(hours=24)
FINISHED = ("done", "skipped")   # follow-up answers that close an OA


def _parse(iso):
    try:
        return gcal.parse(iso) if iso else None
    except (TypeError, ValueError, AttributeError):
        return None


def _fmt(dt):
    dt = dt.astimezone(config.TZ)
    return f"{dt:%a %b} {dt.day}, {dt.hour % 12 or 12}:{dt:%M %p}"


def block_ended(row: dict, now: datetime) -> bool:
    end = _parse(row.get("event_end"))
    return row.get("status") == "scheduled" and end is not None and end <= now


def at_risk(row: dict, now: datetime) -> str | None:
    """Why this OA might be missed, or None. Only OAs due within the next 24 hours."""
    if row.get("kind") != "oa" or row.get("followup") in FINISHED:
        return None
    due = _parse(row.get("deadline"))
    if not due or not now < due <= now + RISK_WINDOW:
        return None
    status = row.get("status")
    if status == "error":
        return "nothing could be blocked for it"
    if status == "dismissed":
        return "you dismissed its block (if you already took it, mark it done)"
    if block_ended(row, now):
        return "its block has passed and it isn't marked done"
    return None


def _hours(due, now) -> str:
    h = max(int((due - now).total_seconds() // 3600), 0)
    return "under an hour" if h < 1 else f"{h} hour{'s' if h != 1 else ''}"


def check_once(now: datetime | None = None) -> list[str]:
    """One pass. Returns the messages sent (for tests and the log)."""
    now = now or datetime.now(config.TZ)
    sent = []
    for row in db.items_of_kind("oa", ("scheduled", "dismissed", "error")):
        name = f"{row['company']} OA" if row.get("company") else "OA"
        if block_ended(row, now) and not row.get("followup"):
            with pipeline._state_lock:  # the student may be answering this very card right now
                row = db.get_item(row["id"])
                fresh = row["status"] == "scheduled" and not row["followup"]
                if fresh:
                    db.update_item(row["id"], followup="asked")
                    row["followup"] = "asked"
            if fresh:
                sent.append(f"Your {name} block just ended. Did you take it? "
                        "Mark it Done, Skipped or Reschedule on the dashboard.")
        why = at_risk(row, now)
        if why and row.get("risk_alerted") != row.get("deadline"):
            db.update_item(row["id"], risk_alerted=row["deadline"])
            due = _parse(row["deadline"])
            sent.append(f"Heads up: the {name} is due in {_hours(due, now)} ({_fmt(due)}) and {why}.")
    for message in sent:
        notify.send(message)
    return sent


def _loop():
    while True:
        try:
            check_once()
        except Exception:
            traceback.print_exc()
        time.sleep(config.FOLLOWUP_SECONDS)


def start():
    threading.Thread(target=_loop, daemon=True, name="followup").start()
