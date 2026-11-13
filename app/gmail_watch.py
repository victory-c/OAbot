"""Background loop: check Gmail every few seconds, feed new mail to the pipeline."""
import base64
import threading
import time

from . import config, db, pipeline
from .google_auth import load_credentials


def _body(payload) -> str:
    """Pull the plain-text body out of a Gmail message payload."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        text = _body(part)
        if text:
            return text
    return ""


def _poll_once(svc, first_run: bool):
    resp = svc.users().messages().list(userId="me", q=config.GMAIL_QUERY, maxResults=25).execute()
    for ref in resp.get("messages", []):
        if db.is_seen(ref["id"]):
            continue
        db.mark_seen(ref["id"])
        if first_run:
            continue  # only act on mail that arrives after startup
        msg = svc.users().messages().get(userId="me", id=ref["id"], format="full").execute()
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        body = _body(msg["payload"]) or msg.get("snippet", "")
        pipeline.submit(headers.get("subject", "(no subject)"), headers.get("from", ""), body[:20000], source="gmail")


def _loop():
    from googleapiclient.discovery import build

    svc = build("gmail", "v1", credentials=load_credentials(), cache_discovery=False)
    first = True
    while True:
        try:
            _poll_once(svc, first)
            first = False
        except Exception as exc:
            print(f"[OAbot] Gmail poll failed: {exc}", flush=True)
        time.sleep(config.POLL_SECONDS)


def start() -> bool:
    """Start the watcher if Google is connected. Returns whether it started."""
    if load_credentials() is None:
        return False
    threading.Thread(target=_loop, daemon=True).start()
    return True
