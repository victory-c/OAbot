"""Run:  uvicorn app.main:app --reload --port 8000"""
import html as htmllib
import re
from contextlib import asynccontextmanager
from pathlib import Path

import markdown
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, gcal, gmail_watch, pipeline

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
            "bcourses": bool(config.BCOURSES_ICS_URL), "model": config.MODEL}


@app.post("/api/ingest")
def ingest(email: EmailIn):
    if not email.body.strip():
        raise HTTPException(400, "Empty email")
    return {"id": pipeline.submit(email.subject, email.sender, email.body, source="paste")}


@app.get("/api/items")
def items():
    rows = db.list_items()
    for r in rows:
        r["has_prep"] = bool(r.pop("prep_md"))
        r.pop("body", None)
    return rows


@app.get("/api/samples")
def samples():
    out = {}
    for path in sorted((config.ROOT / "samples").glob("*_email.txt")):
        subject, _, body = path.read_text().partition("\n\n")
        out[path.stem.replace("_email", "")] = {"subject": subject.removeprefix("Subject: "), "body": body}
    return out


@app.get("/prep/{item_id}", response_class=HTMLResponse)
def prep(item_id: int):
    item = db.get_item(item_id)
    if not item or not item["prep_md"]:
        raise HTTPException(404, "No prep doc yet")
    # The doc is built from web content: neutralise raw HTML and script links before rendering.
    html = markdown.markdown(item["prep_md"].replace("<", "&lt;"), extensions=["tables", "sane_lists"])
    html = re.sub(r'href="(?!https?://)[^"]*"', 'href="#"', html)
    return render("prep.html", title=htmllib.escape(f"{item['company']} prep"), content=html)
