"""Workload from bCourses (Canvas). Every student has a calendar feed URL:
bCourses -> Calendar -> "Calendar Feed". Put it in .env as BCOURSES_ICS_URL."""
import threading
import time as _time
import urllib.request
from datetime import date, datetime, time, timedelta

from . import config

_CACHE_SECONDS = 600  # the feed changes rarely; one email triggers several lookups
_cache: dict = {"url": None, "at": 0.0, "events": None}
_lock = threading.Lock()


def _mock(start: datetime):
    d = start.astimezone(config.TZ).replace(minute=0, second=0, microsecond=0)
    return [
        {"title": "CS 170 Midterm 2", "due": (d + timedelta(days=2)).replace(hour=19).isoformat(), "mock": True},
        {"title": "DATA 100 Homework 9", "due": (d + timedelta(days=3)).replace(hour=23, minute=59).isoformat(), "mock": True},
        {"title": "CS 161 Project 3", "due": (d + timedelta(days=6)).replace(hour=23, minute=59).isoformat(), "mock": True},
    ]


def _feed_url(url: str) -> str:
    """Calendar apps hand out webcal:// links; it is plain https underneath."""
    if url.lower().startswith("webcal://"):
        return "https://" + url[len("webcal://"):]
    return url


def parse_feed(data: bytes) -> list[dict]:
    """Every dated event in an ICS feed as [{title, due}], due in the student's timezone."""
    from icalendar import Calendar

    out = []
    for ev in Calendar.from_ical(data).walk("VEVENT"):
        if "DTSTART" not in ev:
            continue
        raw = ev.decoded("DTSTART")
        if isinstance(raw, datetime):
            due = raw if raw.tzinfo else raw.replace(tzinfo=config.TZ)
        elif isinstance(raw, date):  # all-day: counts as due at the end of that day
            due = datetime.combine(raw, time(23, 59), tzinfo=config.TZ)
        else:
            continue
        title = str(ev.get("SUMMARY", "")).strip() or "Untitled bCourses item"
        out.append({"title": title, "due": due.astimezone(config.TZ).isoformat()})
    return sorted(out, key=lambda d: d["due"])


def _feed_events() -> list[dict]:
    """The parsed feed, cached for a few minutes. If bCourses is down, the last good copy is used;
    with no copy at all the error is raised (the scheduler carries on without coursework)."""
    url = _feed_url(config.BCOURSES_ICS_URL)
    with _lock:
        fresh = _cache["url"] == url and _time.monotonic() - _cache["at"] < _CACHE_SECONDS
        if fresh and _cache["events"] is not None:
            return _cache["events"]
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "OAbot"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                events = parse_feed(resp.read())
        except Exception as exc:
            if _cache["url"] == url and _cache["events"] is not None:
                # never the message: it can carry the private feed URL
                print(f"[OAbot] bCourses feed unavailable ({type(exc).__name__}); using the last copy", flush=True)
                return _cache["events"]
            raise
        _cache.update(url=url, at=_time.monotonic(), events=events)
        return events


def upcoming_deadlines(start: datetime, end: datetime) -> list[dict]:
    """Assignments and exams due between start and end as [{title, due}]."""
    # look a little past the window: something due right after it still eats time inside it
    until = end + timedelta(days=2)
    if not config.BCOURSES_ICS_URL:
        return [d for d in _mock(start) if start <= datetime.fromisoformat(d["due"]) <= until]
    return [d for d in _feed_events() if start <= datetime.fromisoformat(d["due"]) <= until]
