"""Calendar access. Real Google Calendar if logged in, otherwise a local mock
so everyone can build and demo without Google login.

Check what the scheduler sees on your real calendar:  python -m app.gcal [days]
"""
import json
import uuid
from datetime import datetime, timedelta

from . import config
from .google_auth import load_credentials

MOCK_FILE = config.DATA / "mock_calendar.json"


def _service():
    creds = load_credentials()
    if not creds:
        return None
    from googleapiclient.discovery import build

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def using_mock() -> bool:
    return load_credentials() is None


def _mock_created():
    return json.loads(MOCK_FILE.read_text()) if MOCK_FILE.exists() else []


def _mock_classes(start: datetime, end: datetime):
    """A plausible Berkeley week: lectures, a discussion, a club meeting."""
    weekly = {
        0: [("CS 170 Lecture", 10, 0, 11, 0), ("DATA 100 Lecture", 14, 0, 15, 30)],
        1: [("CS 161 Lecture", 11, 0, 12, 30), ("CS 170 Discussion", 16, 0, 17, 0)],
        2: [("CS 170 Lecture", 10, 0, 11, 0), ("DATA 100 Lecture", 14, 0, 15, 30), ("Club meeting", 19, 0, 20, 0)],
        3: [("CS 161 Lecture", 11, 0, 12, 30), ("DATA 100 Lab", 15, 0, 17, 0)],
        4: [("CS 170 Lecture", 10, 0, 11, 0)],
    }
    out = []
    day = start.astimezone(config.TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    while day < end:
        for title, h1, m1, h2, m2 in weekly.get(day.weekday(), []):
            out.append({"title": title, "start": day.replace(hour=h1, minute=m1).isoformat(),
                        "end": day.replace(hour=h2, minute=m2).isoformat()})
        day += timedelta(days=1)
    return out


def _calendar_ids(svc) -> list[tuple[str, str]]:
    """(id, name) of every calendar shown in the user's Google Calendar, primary first.

    Class schedules often live on a secondary or subscribed calendar, so reading only
    'primary' would miss them. Needs the calendar.readonly scope; without it, primary only.
    """
    try:
        cals, token = [], None
        while True:
            resp = svc.calendarList().list(pageToken=token, minAccessRole="reader").execute()
            cals += resp.get("items", [])
            token = resp.get("nextPageToken")
            if not token:
                break
    except Exception as exc:
        print(f"[OAbot] Could not list calendars, using primary only: {exc}", flush=True)
        return [("primary", "primary")]
    picked = [c for c in cals if c.get("primary") or (c.get("selected") and not c.get("hidden"))]
    picked.sort(key=lambda c: not c.get("primary"))
    return [("primary" if c.get("primary") else c["id"], c.get("summaryOverride") or c.get("summary", c["id"]))
            for c in picked] or [("primary", "primary")]


def _is_busy(e: dict) -> bool:
    """Timed, confirmed, not marked 'free', and not declined by the user."""
    if e.get("status") == "cancelled" or "dateTime" not in e.get("start", {}):
        return False  # cancelled, or an all-day event (holidays, "exam week" banners)
    if e.get("transparency") == "transparent":
        return False  # the user marked it "Free"
    me = next((a for a in e.get("attendees", []) if a.get("self")), None)
    return not (me and me.get("responseStatus") == "declined")


def _google_events(svc, start: datetime, end: datetime) -> list[dict]:
    events = []
    for cal_id, cal_name in _calendar_ids(svc):
        token = None
        try:
            while True:
                resp = svc.events().list(
                    calendarId=cal_id, timeMin=start.isoformat(), timeMax=end.isoformat(),
                    singleEvents=True,  # expands recurring events (weekly lectures) into each occurrence
                    orderBy="startTime", maxResults=250, pageToken=token, timeZone=config.TZ.key,
                ).execute()
                for e in resp.get("items", []):
                    if _is_busy(e):
                        item = {"title": e.get("summary", "(busy)"), "start": e["start"]["dateTime"],
                                "end": e["end"]["dateTime"]}
                        if cal_id != "primary":
                            item["calendar"] = cal_name
                        events.append(item)
                token = resp.get("nextPageToken")
                if not token:
                    break
        except Exception as exc:  # one broken subscribed calendar must not hide the rest
            print(f"[OAbot] Skipping calendar {cal_name!r}: {exc}", flush=True)
    return events


def list_busy(start: datetime, end: datetime) -> list[dict]:
    """Timed events between start and end as [{title, start, end}] (ISO strings), local time."""
    svc = _service()
    if svc is None:
        events = _mock_classes(start, end) + [
            {k: e[k] for k in ("title", "start", "end")} for e in _mock_created()]
    else:
        events = _google_events(svc, start, end)
    keep = [e for e in events if parse(e["end"]) > start and parse(e["start"]) < end]
    for e in keep:  # one timezone for everything the scheduler reads
        e["start"] = parse(e["start"]).astimezone(config.TZ).isoformat()
        e["end"] = parse(e["end"]).astimezone(config.TZ).isoformat()
    return sorted(keep, key=lambda e: parse(e["start"]))


def insert_event(title: str, start: datetime, end: datetime, description: str = "") -> dict:
    """Create the block. Returns {id, link} (link is '' in mock mode)."""
    svc = _service()
    if svc is None:
        events = _mock_created()
        event_id = f"mock-{uuid.uuid4().hex[:12]}"
        events.append({"id": event_id, "title": title, "start": start.isoformat(), "end": end.isoformat()})
        MOCK_FILE.write_text(json.dumps(events, indent=2))
        return {"id": event_id, "link": ""}
    body = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start.isoformat(), "timeZone": config.TZ.key},
        "end": {"dateTime": end.isoformat(), "timeZone": config.TZ.key},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]},
        "colorId": "11",
    }
    created = svc.events().insert(calendarId="primary", body=body).execute()
    return {"id": created.get("id", ""), "link": created.get("htmlLink", "")}


def create_event(title: str, start: datetime, end: datetime, description: str = "") -> str:
    """Create the block. Returns a link to it ('' in mock mode)."""
    return insert_event(title, start, end, description)["link"]


def delete_event(event_id: str) -> bool:
    """Remove a block OAbot created. Returns True if it was there and is now gone."""
    if not event_id:
        return False
    if event_id.startswith("mock-"):
        events = _mock_created()
        kept = [e for e in events if e.get("id") != event_id]
        MOCK_FILE.write_text(json.dumps(kept, indent=2))
        return len(kept) < len(events)
    svc = _service()
    if svc is None:
        return False
    from googleapiclient.errors import HttpError

    try:
        svc.events().delete(calendarId="primary", eventId=event_id).execute()
        return True
    except HttpError as exc:
        if exc.resp.status in (404, 410):  # already deleted by hand
            return False
        raise


def parse(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=config.TZ)


if __name__ == "__main__":
    import sys

    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    now = datetime.now(config.TZ)
    svc = _service()
    if svc is None:
        print("Not logged in to Google (mock mode). Run: python -m app.google_auth")
    else:
        print("Calendars read:", ", ".join(name for _, name in _calendar_ids(svc)))
    print(f"Busy time for the next {days} days ({config.TZ.key}):\n")
    for e in list_busy(now, now + timedelta(days=days)):
        s, t = parse(e["start"]), parse(e["end"])
        where = f"  [{e['calendar']}]" if e.get("calendar") else ""
        print(f"  {s:%a %b %d  %H:%M}-{t:%H:%M}  {e['title']}{where}")
