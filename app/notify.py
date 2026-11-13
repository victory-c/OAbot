"""Tell the student what just happened. Console always; Discord if a webhook is set."""
import json
import re
import threading
import urllib.request

from . import config

DISCORD_LIMIT = 2000
TIMEOUT = 8  # seconds
_DUE_IN = re.compile(r"\bdue in (?:under |about |around |less than |roughly |~)?(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?)\b", re.I)
_DUE_SOON = re.compile(r"\bdue (?:in |with)?(?:under|less than|within) (?:24 hours|a day|an hour)\b", re.I)


def is_urgent(message: str) -> bool:
    """True when the text says the thing is due within 24 hours. The scheduler rounds the time
    left, so its urgent line for 23.5 to 24 hours reads "Due in 24 hours": that counts too."""
    if _DUE_SOON.search(message):
        return True
    for amount, unit in _DUE_IN.findall(message):
        if unit.lower().startswith("m") or float(amount) <= 24:
            return True
    return False


def format_message(message: str) -> str:
    """Lock-screen friendly: headline first, the scheduler's reasons as short lines, at most 2000 characters."""
    text = (message or "").strip()
    parts = [p.strip() for p in re.split(r";\s+|\s*\n+\s*", text) if p.strip()]
    if len(parts) > 1:
        # pipeline.py sends "<headline>. <reason>; <reason>; ...": the first reason rides on the headline
        head, sep, first = parts[0].rpartition(". ")
        if sep and first:
            headline, reasons = head + ".", [first] + parts[1:]
        else:
            headline, reasons = parts[0], parts[1:]
        text = "\n".join([headline] + ["- " + r.lstrip("-\u2022 ").rstrip(".") for r in reasons])
    if is_urgent(text) and not text.upper().startswith("URGENT"):
        text = "URGENT: " + text
    if len(text) > DISCORD_LIMIT:
        text = text[:DISCORD_LIMIT - 3].rstrip() + "..."
    return text


def _post(url: str, content: str):
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "OAbot"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT):
            pass
    except Exception as exc:  # a failed ping must never break the pipeline
        print(f"[OAbot] Discord notify failed: {type(exc).__name__}: {exc}", flush=True)


def send(message: str) -> None:
    try:
        print(f"[OAbot] {message}", flush=True)
        url = config.DISCORD_WEBHOOK_URL
        if not url:
            return
        # off the pipeline's thread: a slow webhook must not delay the next email
        threading.Thread(target=_post, args=(url, format_message(str(message))), name="oabot-notify").start()
    except Exception as exc:
        try:
            print(f"[OAbot] notify failed: {type(exc).__name__}: {exc}", flush=True)
        except Exception:
            pass
