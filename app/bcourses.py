"""Workload from bCourses (Canvas). Every student has a calendar feed URL:
bCourses -> Calendar -> "Calendar Feed". Put it in .env as BCOURSES_ICS_URL."""
import urllib.request
from datetime import date, datetime, time, timedelta

from . import config


def _mock(start: datetime):
    d = start.astimezone(config.TZ).replace(minute=0, second=0, microsecond=0)
    return [
        {"title": "CS 170 Midterm 2", "due": (d + timedelta(days=2)).replace(hour=19).isoformat(), "mock": True},
        {"title": "DATA 100 Homework 9", "due": (d + timedelta(days=3)).replace(hour=23, minute=59).isoformat(), "mock": True},
        {"title": "CS 161 Project 3", "due": (d + timedelta(days=6)).replace(hour=23, minute=59).isoformat(), "mock": True},
    ]


def upcoming_deadlines(start: datetime, end: datetime) -> list[dict]:
    """Assignments and exams due between start and end as [{title, due}]."""
    if not config.BCOURSES_ICS_URL:
        return [d for d in _mock(start) if start <= datetime.fromisoformat(d["due"]) <= end + timedelta(days=2)]
    from icalendar import Calendar

    with urllib.request.urlopen(config.BCOURSES_ICS_URL, timeout=15) as resp:
        cal = Calendar.from_ical(resp.read())
    out = []
    for ev in cal.walk("VEVENT"):
        raw = ev.decoded("DTSTART")
        if isinstance(raw, datetime):
            due = raw if raw.tzinfo else raw.replace(tzinfo=config.TZ)
        elif isinstance(raw, date):
            due = datetime.combine(raw, time(23, 59), tzinfo=config.TZ)
        else:
            continue
        # look a little past the window: something due right after it still eats time inside it
        if start <= due <= end + timedelta(days=2):
            out.append({"title": str(ev.get("SUMMARY", "")), "due": due.astimezone(config.TZ).isoformat()})
    return sorted(out, key=lambda d: d["due"])
