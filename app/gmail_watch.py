"""Background loop: check Gmail every few seconds, feed new mail to the pipeline."""
import base64
import html
import re
import threading
import time
from html.parser import HTMLParser

from . import config, db, notify, pipeline
from .google_auth import load_credentials

MAX_PAGES = 10          # up to 250 matching messages per poll
GIVE_UP_AFTER = 5       # failed fetches of one message before it is skipped (with a ping)
_failures: dict[str, int] = {}

# A text/plain part shorter than this is often a stub ("view this email in your browser"),
# so when there is an HTML part as well we read that instead.
MIN_PLAIN_CHARS = 200


class _HTMLToText(HTMLParser):
    """Turn an HTML email into readable plain text.

    Keeps link targets ("Start assessment (https://...)") because the OA link usually
    lives only in a button's href, and the classifier needs it.
    """

    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
             "table", "section", "article", "header", "footer", "blockquote", "hr", "ul", "ol"}
    SKIP = {"script", "style", "head", "title", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0
        self._links: list[tuple[str, int]] = []  # (href, position in out when <a> opened)

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        if tag == "li":
            self.out.append("- ")
        elif tag == "a":
            self._links.append((dict(attrs).get("href") or "", len(self.out)))
        elif tag in ("td", "th"):
            self.out.append(" ")

    def handle_startendtag(self, tag, attrs):
        if tag in ("br", "hr"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._links:
            href, pos = self._links.pop()
            text = "".join(self.out[pos:]).strip()
            if href.lower().startswith(("http://", "https://")) and href not in text:
                self.out.append(f" ({href})")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)

    def text(self) -> str:
        raw = "".join(self.out).replace("\xa0", " ")  # entities already decoded (convert_charrefs)
        lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in raw.split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(markup: str) -> str:
    parser = _HTMLToText()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # malformed HTML: fall back to stripping tags
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", markup))).strip()
    return parser.text()


def _decode(part) -> str:
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    charset = "utf-8"
    for h in part.get("headers", []) or []:
        if h.get("name", "").lower() == "content-type":
            m = re.search(r'charset="?([\w.-]+)', h.get("value", ""), re.I)
            if m:
                charset = m.group(1)
    try:
        return raw.decode(charset, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def _collect(payload, found: dict):
    """Find the first text/plain and first text/html parts, skipping attachments."""
    if payload.get("filename"):
        return
    mime = payload.get("mimeType", "")
    if mime in ("text/plain", "text/html") and mime not in found:
        text = _decode(payload)
        if text.strip():
            found[mime] = text
    for part in payload.get("parts", []) or []:
        _collect(part, found)


def _body(payload) -> str:
    """Readable body of a Gmail message payload: text/plain, or the HTML part as text."""
    found: dict = {}
    _collect(payload, found)
    plain = found.get("text/plain", "").strip()
    markup = found.get("text/html", "")
    if markup and len(plain) < MIN_PLAIN_CHARS:
        converted = html_to_text(markup)
        if len(converted) > len(plain):
            return converted
    return plain


def _list_ids(svc) -> list[str]:
    """Every message matching the query, following pages, oldest first (so a reminder comes after its OA)."""
    ids, token = [], None
    for _ in range(MAX_PAGES):
        resp = svc.users().messages().list(userId="me", q=config.GMAIL_QUERY, maxResults=25,
                                           **({"pageToken": token} if token else {})).execute()
        ids += [ref["id"] for ref in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token:
            break
    return list(reversed(ids))


def _process(svc, gmail_id: str):
    msg = svc.users().messages().get(userId="me", id=gmail_id, format="full").execute()
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    body = _body(msg["payload"]) or html.unescape(msg.get("snippet", ""))
    subject = headers.get("subject", "(no subject)")
    print(f"[OAbot] New mail: {subject!r}", flush=True)
    pipeline.submit(subject, headers.get("from", ""), body[:20000], source="gmail")


def _gone(exc) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    return str(status) in ("404", "410")


def _poll_once(svc):
    """One pass. The very first time Gmail is connected, mail already in the inbox is only
    noted, not acted on. After that every new message is processed, including any that
    arrived while the server was off. A message counts as seen only once it is queued, so
    a failed fetch is retried on the next poll instead of being dropped."""
    ids = _list_ids(svc)
    if db.get_meta("gmail_started") is None:
        for gmail_id in ids:
            db.mark_seen(gmail_id)
        db.set_meta("gmail_started", "1")
        return
    for gmail_id in ids:
        if db.is_seen(gmail_id):
            continue
        try:
            _process(svc, gmail_id)
        except Exception as exc:
            tries = _failures[gmail_id] = _failures.get(gmail_id, 0) + 1
            print(f"[OAbot] Could not read Gmail message {gmail_id} (try {tries}): {exc}", flush=True)
            if _gone(exc):
                db.mark_seen(gmail_id)  # deleted before we read it: nothing to do
            elif tries >= GIVE_UP_AFTER:
                db.mark_seen(gmail_id)
                notify.send("Could not read a new email after several tries, so it was skipped. "
                            "If a recruiting email is missing from the dashboard, paste it in.")
            continue
        _failures.pop(gmail_id, None)
        db.mark_seen(gmail_id)


def _loop():
    from googleapiclient.discovery import build

    svc = build("gmail", "v1", credentials=load_credentials(), cache_discovery=False)
    while True:
        try:
            _poll_once(svc)
        except Exception as exc:
            print(f"[OAbot] Gmail poll failed: {exc}", flush=True)
        time.sleep(config.POLL_SECONDS)


def start() -> bool:
    """Start the watcher if Google is connected. Returns whether it started."""
    if load_credentials() is None:
        return False
    threading.Thread(target=_loop, daemon=True).start()
    return True
