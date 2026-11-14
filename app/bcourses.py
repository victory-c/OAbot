"""Workload from bCourses (Canvas). Every student has a calendar feed URL:
bCourses -> Calendar -> "Calendar Feed". Put it in .env as BCOURSES_ICS_URL.

Check what the scheduler will see with:  python -m app.bcourses [days]

A broken feed never breaks scheduling: any fetch or parse problem prints one
"[OAbot] ..." warning and yields no deadlines."""
import re
import sys
import time as _time
import urllib.request
from datetime import date, datetime, time, timedelta
from urllib.parse import urlsplit

from . import config

TIMEOUT = 6          # seconds; the scheduler waits on this
CACHE_SECONDS = 300  # a good fetch is reused for this long
RETRY_SECONDS = 60   # after a failure, do not hammer (or wait on) the feed again for this long
_ASSIGNMENT_UID = "event-assignment-"
_CANVAS_UIDS = (_ASSIGNMENT_UID, "event-calendar-event-")
_EXAM = re.compile(r"\b(exams?|midterms?|quiz(?:zes)?|finals?(?!\s+(?:project|report|paper|draft|presentation|essay)))\b", re.I)

_cache: dict = {}  # url -> (fetched_at monotonic, ok, [(title, due)])


def _is_exam(title: str) -> bool:
    return bool(_EXAM.search(title or ""))


def _entry(title: str, due: datetime, **extra) -> dict:
    out = {"title": title, "due": due.astimezone(config.TZ).isoformat(), **extra}
    if _is_exam(title):
        out["exam"] = True
    return out


# Demo coursework, pinned to weekdays so the week always looks the same: the midterm on a
# Tuesday afternoon (between CS 161 lecture and CS 170 discussion), homework piling up on the weekend.
_MOCK_WEEK = [  # (title, weekday Mon=0, hour, minute)
    ("CS 161 Homework 6", 5, 19, 0),
    ("CS 170 Homework 11", 5, 23, 59),
    ("DATA 100 Lab 10", 6, 17, 0),
    ("DATA 100 Homework 9", 6, 23, 59),
    ("CS 170 Midterm 2", 1, 14, 0),
    ("CS 161 Project 3", 3, 23, 59),
]


def _mock(start: datetime):
    """The next occurrence of each demo item, within the 7 days from start's date."""
    today = start.astimezone(config.TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for title, weekday, hour, minute in _MOCK_WEEK:
        day = today + timedelta(days=(weekday - today.weekday()) % 7)
        out.append(_entry(title, day.replace(hour=hour, minute=minute), mock=True))
    return sorted(out, key=lambda e: e["due"])


def _warn(msg: str):
    print(f"[OAbot] bCourses feed: {msg}. Scheduling continues without coursework deadlines.", flush=True)


def _normalize(url: str) -> str:
    url = url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    return url


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "OAbot"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        status = getattr(resp, "status", 200)
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        return resp.read()


def _parse(body: bytes) -> list[tuple[str, datetime]]:
    """All deadlines in a Canvas feed as (title, due in config.TZ). Raises on a body that is not a calendar."""
    from icalendar import Calendar

    if b"BEGIN:VCALENDAR" not in body[:2000].upper():
        raise ValueError("response is not an iCalendar file (is the feed URL right?)")
    cal = Calendar.from_ical(body)
    events = []
    for ev in cal.walk("VEVENT"):
        try:
            raw = ev.decoded("DTSTART")
        except Exception:
            continue
        if isinstance(raw, datetime):
            due = raw if raw.tzinfo else raw.replace(tzinfo=config.TZ)
        elif isinstance(raw, date):  # all-day assignment: due at the end of that day
            due = datetime.combine(raw, time(23, 59), tzinfo=config.TZ)
        else:
            continue
        events.append((str(ev.get("UID", "")), str(ev.get("SUMMARY", "")).strip(), due.astimezone(config.TZ)))
    # Canvas tags assignments in the UID; lectures and office hours share the feed and are not deadlines.
    if any(uid.startswith(_CANVAS_UIDS) for uid, _, _ in events):
        events = [e for e in events if e[0].startswith(_ASSIGNMENT_UID)]
    return [(title, due) for _, title, due in events]


def _feed(url: str) -> list[tuple[str, datetime]]:
    """Parsed feed, cached. Never raises: a failure warns once and returns the last good copy or []."""
    now = _time.monotonic()
    hit = _cache.get(url)
    if hit and now - hit[0] < (CACHE_SECONDS if hit[1] else RETRY_SECONDS):
        return hit[2]
    try:
        events = _parse(_fetch(url))
    except Exception as exc:
        stale = hit[2] if hit else []
        _warn(f"could not be read ({type(exc).__name__}: {exc})"
              + (", using the last good copy" if stale else ""))
        _cache[url] = (now, False, stale)
        return stale
    _cache[url] = (now, True, events)
    return events


def clear_cache():
    _cache.clear()


def upcoming_deadlines(start: datetime, end: datetime) -> list[dict]:
    """Assignments and exams due between start and end as [{title, due}], sorted by due."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=config.TZ)
    if end.tzinfo is None:
        end = end.replace(tzinfo=config.TZ)
    # look a little past the window: something due right after it still eats time inside it
    last = end + timedelta(days=2)
    if not config.BCOURSES_ICS_URL:
        return [d for d in _mock(start) if start <= datetime.fromisoformat(d["due"]) <= last]
    try:
        found = sorted(((due, title) for title, due in _feed(_normalize(config.BCOURSES_ICS_URL))
                        if start <= due <= last), key=lambda p: p[0])
        return [_entry(title, due) for due, title in found]
    except Exception as exc:  # belt and braces: coursework must never fail an email
        _warn(f"unexpected problem ({type(exc).__name__}: {exc})")
        return []


def _main(argv: list[str]) -> int:
    try:
        days = int(argv[0]) if argv else 14
    except ValueError:
        print("usage: python -m app.bcourses [days]")
        return 2
    now = datetime.now(config.TZ)
    url = _normalize(config.BCOURSES_ICS_URL) if config.BCOURSES_ICS_URL else ""
    source = f"live feed at {urlsplit(url).hostname}" if url else "MOCK data (BCOURSES_ICS_URL is empty)"
    items = upcoming_deadlines(now, now + timedelta(days=days))
    ok = not url or (_cache.get(url) or (0, False))[1]
    print(f"Source: {source}")
    if not ok:
        print("Status: FEED NOT READABLE, the scheduler will see no coursework. Check the URL in .env.")
    print(f"Deadlines in the next {days} days (plus 2 days of lookahead): {len(items)}")
    for d in items:
        due = datetime.fromisoformat(d["due"])
        print(f"  {due:%a %b %d} {due.hour % 12 or 12}:{due:%M %p}  {'[EXAM] ' if d.get('exam') else ''}{d['title']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
