"""Calendar access. Real Google Calendar if logged in, otherwise a local mock
so everyone can build and demo without Google login."""
import json
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


def list_busy(start: datetime, end: datetime) -> list[dict]:
    """Timed events between start and end as [{title, start, end}] (ISO strings)."""
    svc = _service()
    if svc is None:
        events = _mock_classes(start, end) + _mock_created()
    else:
        resp = svc.events().list(
            calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),
            singleEvents=True, orderBy="startTime", maxResults=250,
        ).execute()
        events = [
            {"title": e.get("summary", "(busy)"), "start": e["start"]["dateTime"], "end": e["end"]["dateTime"]}
            for e in resp.get("items", [])
            if "dateTime" in e.get("start", {})  # skip all-day events
        ]
    keep = [e for e in events if parse(e["end"]) > start and parse(e["start"]) < end]
    return sorted(keep, key=lambda e: parse(e["start"]))


def create_event(title: str, start: datetime, end: datetime, description: str = "") -> str:
    """Create the block. Returns a link to it ('' in mock mode)."""
    svc = _service()
    if svc is None:
        events = _mock_created()
        events.append({"title": title, "start": start.isoformat(), "end": end.isoformat()})
        MOCK_FILE.write_text(json.dumps(events, indent=2))
        return ""
    body = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start.isoformat(), "timeZone": config.TZ.key},
        "end": {"dateTime": end.isoformat(), "timeZone": config.TZ.key},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]},
        "colorId": "11",
    }
    return svc.events().insert(calendarId="primary", body=body).execute().get("htmlLink", "")


def parse(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=config.TZ)
