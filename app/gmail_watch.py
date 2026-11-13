"""Background loop: check Gmail every few seconds, feed new mail to the pipeline."""
import base64
import html
import re
import threading
import time
from html.parser import HTMLParser

from . import config, db, pipeline
from .google_auth import load_credentials

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
        raw = html.unescape("".join(self.out)).replace("\xa0", " ")
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
        body = _body(msg["payload"]) or html.unescape(msg.get("snippet", ""))
        print(f"[OAbot] New mail: {headers.get('subject', '(no subject)')!r}", flush=True)
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
