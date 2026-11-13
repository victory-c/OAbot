"""Run:  uvicorn app.main:app --reload --port 8000"""
import html as htmllib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import markdown
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import bcourses, config, db, followup, gcal, gmail_watch, pipeline, quiz

STATE = {"gmail": False}
TEMPLATES = Path(__file__).parent / "templates"


def render(name: str, **values: str) -> str:
    """Every page gets the same head (ui.css, fonts, favicon) and header from the shared partials."""
    read = lambda n: (TEMPLATES / n).read_text(encoding="utf-8")
    page = read(name).replace("{{head}}", read("_head.html")).replace("{{header}}", read("_header.html"))
    for key, value in values.items():  # in order: put untrusted content last
        page = page.replace("{{" + key + "}}", value)
    return page


@asynccontextmanager
async def lifespan(_app):
    STATE["gmail"] = gmail_watch.start()
    followup.start()
    print(f"[OAbot] Gmail watcher: {'on' if STATE['gmail'] else 'off (paste mode)'} | "
          f"Calendar: {'mock' if gcal.using_mock() else 'Google'}", flush=True)
    yield


app = FastAPI(title="OAbot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


class EmailIn(BaseModel):
    subject: str = ""
    sender: str = ""
    body: str


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return render("dashboard.html")


@app.get("/api/status")
def status():
    return {"gmail": STATE["gmail"], "calendar": "mock" if gcal.using_mock() else "google",
            "bcourses": bool(config.BCOURSES_ICS_URL), "model": config.MODEL, "tz": config.TZ.key}


@app.post("/api/ingest")
def ingest(email: EmailIn):
    if not email.body.strip():
        raise HTTPException(400, "Empty email")
    return {"id": pipeline.submit(email.subject, email.sender, email.body, source="paste")}


@app.get("/api/items")
def items():
    rows = db.list_items()
    now = datetime.now(config.TZ)
    for r in rows:
        r["has_prep"] = bool(r.pop("prep_md"))
        r.pop("body", None)
        r["at_risk"] = followup.at_risk(r, now)
        # Ask on the card as soon as the block ends, without waiting for the next background check.
        r["ask_followup"] = r.get("kind") == "oa" and followup.block_ended(r, now) and r.get("followup") in (None, "asked")
        start = followup._parse(r.get("event_start"))
        r["can_move"] = bool(start and start > now and (
            (r.get("kind") == "oa" and r.get("status") == "scheduled")
            or (r.get("kind") == "interview_scheduled" and r.get("status") == "prepped")))
    return rows


class FollowupIn(BaseModel):
    answer: str


@app.post("/api/items/{item_id}/followup")
def answer_followup(item_id: int, body: FollowupIn):
    """Done / Skipped / Reschedule on an OA whose block has passed."""
    result = pipeline.answer_followup(item_id, body.answer)
    if not result["ok"]:
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/items/{item_id}/move")
def move_block(item_id: int):
    """Move an OA or prep block that is still ahead to the next best slot."""
    result = pipeline.move_block(item_id)
    if not result["ok"]:
        raise HTTPException(400, result["error"])
    return result


# --- "This week" strip -------------------------------------------------------------------
# Read-only. Each outside source (Google Calendar, the bCourses feed) is fetched on a worker
# thread with a short wait and a small cache, so a slow or broken source never blocks the
# dashboard or hides the other sources. OAbot's own blocks come straight from the database.
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="calendar")
_sources: dict = {}
_sources_lock = threading.Lock()


def _source(key: str, fn, ttl: float, wait: float = 4.0):
    """Returns (value, problem). On failure or timeout: the last good value plus a short reason."""
    now = time.monotonic()
    with _sources_lock:
        slot = _sources.setdefault(key, {"until": 0.0, "value": [], "future": None})
        if now < slot["until"]:
            return slot["value"], slot.get("problem")
        if slot["future"] is None:
            slot["future"] = _pool.submit(fn)
        future = slot["future"]
    try:
        value = future.result(timeout=wait)
    except FutureTimeout:
        return slot["value"], "slow to answer"
    except Exception as exc:  # never the message: it can carry the private feed URL
        with _sources_lock:
            slot.update(future=None, until=time.monotonic() + 15, problem=f"unavailable ({type(exc).__name__})")
        return slot["value"], slot["problem"]
    with _sources_lock:
        slot.update(future=None, until=time.monotonic() + ttl, value=value, problem=None)
    return value, None


def _place(start_iso: str, end_iso: str, day0: datetime):
    """Where an event sits in the 7-day strip: (day index, start minute, end minute) or None."""
    try:
        start = gcal.parse(start_iso).astimezone(config.TZ)
        end = gcal.parse(end_iso).astimezone(config.TZ)
    except (TypeError, ValueError, AttributeError):
        return None
    day = (start.date() - day0.date()).days
    if not 0 <= day < 7:
        return None
    start_min = start.hour * 60 + start.minute
    end_min = end.hour * 60 + end.minute if end.date() == start.date() else 24 * 60
    return day, start_min, max(end_min, start_min + 15)


@app.get("/api/calendar")
def calendar():
    day0 = datetime.now(config.TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    end = day0 + timedelta(days=7)
    mock = gcal.using_mock()
    problems = {}

    busy, problem = _source(f"busy:{day0.date()}", lambda: gcal.list_busy(day0, end), ttl=1 if mock else 10)
    if problem:
        problems["calendar"] = problem
    due, problem = _source(f"due:{day0.date()}", lambda: bcourses.upcoming_deadlines(day0, end),
                           ttl=1 if not config.BCOURSES_ICS_URL else 120)
    if problem:
        problems["coursework"] = problem

    blocks, mine = [], set()
    try:
        rows = db.list_items()
    except Exception as exc:
        rows, problems["blocks"] = [], f"unavailable ({type(exc).__name__})"
    for r in rows:
        if not r.get("event_start") or not r.get("event_end"):
            continue
        if r.get("status") in ("duplicate", "replaced", "dismissed"):
            continue  # these rows copy or outlive a block they no longer own; drawing them would show it twice or after deletion
        mine.add((r.get("event_title"), r["event_start"]))
        spot = _place(r["event_start"], r["event_end"], day0)
        if spot:
            blocks.append({"id": r["id"], "title": r.get("event_title") or "", "company": r.get("company") or "",
                           "kind": r.get("kind") or "", "start": r["event_start"], "end": r["event_end"],
                           "day": spot[0], "start_min": spot[1], "end_min": spot[2]})

    def same_instant(a, b):
        try:
            return gcal.parse(a) == gcal.parse(b)
        except (TypeError, ValueError):
            return False

    events = []
    for e in busy:
        title = str(e.get("title") or "(busy)")
        # OAbot's blocks also live on the calendar itself: show each once, as a block.
        if any(title == t and same_instant(e.get("start"), s) for t, s in mine):
            continue
        spot = _place(e.get("start"), e.get("end"), day0)
        if spot:
            events.append({"title": title, "start": e["start"], "end": e["end"],
                           "day": spot[0], "start_min": spot[1], "end_min": spot[2],
                           "oabot": title.startswith(("OA: ", "Prep: "))})

    deadlines = []
    for d in due:
        spot = _place(d.get("due"), d.get("due"), day0)
        if spot:
            deadlines.append({"title": str(d.get("title") or ""), "due": d["due"], "day": spot[0], "min": spot[1]})

    days = [{"date": f"{d:%Y-%m-%d}", "weekday": f"{d:%a}", "day": d.day}
            for d in (day0 + timedelta(days=i) for i in range(7))]
    return {"tz": config.TZ.key, "days": days, "busy": events, "deadlines": deadlines, "blocks": blocks,
            "problems": problems, "mock": {"calendar": mock, "coursework": not config.BCOURSES_ICS_URL}}


@app.get("/api/samples")
def samples():
    out = {}
    for path in sorted((config.ROOT / "samples").glob("*_email.txt")):
        subject, _, body = path.read_text().partition("\n\n")
        out[path.stem.replace("_email", "")] = {"subject": subject.removeprefix("Subject: "), "body": body}
    # One interview email per judge, so the judge move works from the dashboard when Gmail is off.
    for path in sorted((config.ROOT / "samples" / "judges").glob("*.txt")):
        subject, _, body = path.read_text().partition("\n\n")
        # "voicebit-jay-patel" -> "judge Jay Patel (VoiceBit)"; the company's spelling comes from its prep doc title
        words = path.stem.split("-")
        name = " ".join(w.capitalize() for w in words[-2:])
        company = " ".join(w.capitalize() for w in words[:-2])
        doc = config.ROOT / "samples" / "prep_cache" / f"{path.stem}.md"
        if doc.exists():
            company = doc.read_text().splitlines()[0].removeprefix("# ").split(":")[0].strip() or company
        out[f"judge {name} ({company})"] = {"subject": subject.removeprefix("Subject: "), "body": body}
    return out


@app.get("/prep/{item_id}", response_class=HTMLResponse)
def prep(item_id: int):
    item = db.get_item(item_id)
    if not item or not item["prep_md"]:
        raise HTTPException(404, "No prep doc yet")
    # The doc is built from web content: neutralise raw HTML and script links before rendering.
    text = item["prep_md"].replace("<", "&lt;")
    # Python-Markdown only starts a list after a blank line. The docs often put bullets straight
    # under a "**Name**" or intro line, which would otherwise render as one run-on paragraph.
    text = re.sub(r"(?m)^(?![-*+] |\d+\. )(\S.*)\n(?=(?:[-*+]|\d+\.) )", r"\1\n\n", text)
    html = markdown.markdown(text, extensions=["tables", "sane_lists"])
    html = re.sub(r"<img\b[^>]*>", "", html)  # no remote images: the page loads nothing from the web
    html = re.sub(r'href="(?!https?://)[^"]*"', 'href="#"', html)
    html = html.replace('<a href="http', '<a target="_blank" rel="noopener noreferrer" href="http')
    html = html.replace("<table>", '<div class="scroll"><table>').replace("</table>", "</table></div>")
    # Everything from a "Sources" / "References" heading on gets its own quiet section.
    html = re.sub(r"<h2>\s*(Sources|References)[^<]*</h2>.*", lambda m: f'<section class="sources">{m.group(0)}</section>',
                  html, count=1, flags=re.S | re.I)
    title = htmllib.escape(f"{item['company'] or 'Interview'} prep").replace("{", "&#123;")
    mode = "oa" if item["kind"] == "oa" else "interview"
    return render("prep.html", item_id=str(int(item_id)), mode=mode, title=title, content=html)


class QuizIn(BaseModel):
    messages: list[dict] = []


@app.post("/api/prep/{item_id}/quiz")
def quiz_turn(item_id: int, body: QuizIn):
    """One turn of "Quiz me". The page holds the transcript; nothing is stored."""
    item = db.get_item(item_id)
    if not item or not item["prep_md"]:
        raise HTTPException(404, "No prep doc yet")
    try:
        messages = quiz.check(body.messages)
    except quiz.BadTranscript as exc:
        raise HTTPException(400, str(exc))
    try:
        text = quiz.reply("oa" if item["kind"] == "oa" else "interview", item["prep_md"], messages)
    except Exception as exc:
        print(f"[quiz] {type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(502, "Claude did not answer. Try again in a moment.")
    return {"reply": text, "done": len(messages) >= 2 * quiz.QUESTIONS - 1}
