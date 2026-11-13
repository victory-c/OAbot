"""Tests for issue #1 (Gmail body extraction, calendar reading, duplicates, dismiss).

Runs offline: mock calendar, Claude calls stubbed, a throwaway data folder.
    python -m unittest discover tests -v
"""
import base64
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402

# Point everything at a temp folder BEFORE db/gcal are imported (they open files at import).
TMP = Path(tempfile.mkdtemp(prefix="oabot-test-"))
config.DATA = TMP
config.GOOGLE_TOKEN = TMP / "token.json"

# An oabot.db from before this change, to prove the migration keeps old rows.
_old = sqlite3.connect(TMP / "oabot.db")
_old.executescript("""
    CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, source TEXT, sender TEXT,
        subject TEXT, body TEXT, status TEXT, kind TEXT, company TEXT, role TEXT, deadline TEXT, link TEXT,
        info_json TEXT, event_title TEXT, event_start TEXT, event_end TEXT, event_link TEXT, reasoning TEXT,
        draft_reply TEXT, prep_md TEXT, error TEXT);
    INSERT INTO items (subject, status) VALUES ('old row', 'ignored');
""")
_old.commit()
_old.close()

from app import classifier, db, gcal, gmail_watch, google_auth, pipeline, researcher, scheduler  # noqa: E402
from app.classifier import EmailInfo  # noqa: E402

TZ = config.TZ


def b64(s: str, charset="utf-8") -> str:
    return base64.urlsafe_b64encode(s.encode(charset)).decode().rstrip("=")  # Gmail omits padding


# ---------------------------------------------------------------- Gmail body

class GmailBody(unittest.TestCase):
    HTML = """<html><head><style>.x{color:red}</style><title>t</title></head><body>
        <table><tr><td><p>Hi&nbsp;Jordan,</p><p>Your <b>Stripe</b> assessment is ready.</p>
        <p>Duration: 90&nbsp;minutes. Due Nov 18.</p>
        <a href="https://hackerrank.com/test/abc/start" class="btn">Start assessment</a>
        <script>track()</script><ul><li>Any language</li><li>Quiet room</li></ul></td></tr></table></body></html>"""

    def test_html_only_email(self):
        payload = {"mimeType": "text/html", "body": {"data": b64(self.HTML)}}
        text = gmail_watch._body(payload)
        self.assertIn("Hi Jordan,", text)
        self.assertIn("Your Stripe assessment is ready.", text)
        self.assertIn("Start assessment (https://hackerrank.com/test/abc/start)", text)
        self.assertIn("- Any language", text)
        for junk in ("track()", "color:red", "<", "&nbsp;"):
            self.assertNotIn(junk, text)

    def test_stub_plain_part_loses_to_html(self):
        payload = {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/plain", "body": {"data": b64("View this email in your browser.")}},
            {"mimeType": "text/html", "body": {"data": b64(self.HTML)}},
        ]}
        self.assertIn("hackerrank.com/test/abc/start", gmail_watch._body(payload))

    def test_real_plain_part_wins(self):
        plain = "Please complete the CodeSignal assessment within 72 hours. " * 5
        payload = {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/plain", "body": {"data": b64(plain)}},
            {"mimeType": "text/html", "body": {"data": b64(self.HTML)}},
        ]}
        self.assertEqual(gmail_watch._body(payload), plain.strip())

    def test_nested_multipart_charset_and_attachment(self):
        payload = {"mimeType": "multipart/mixed", "parts": [
            {"mimeType": "multipart/alternative", "parts": [
                {"mimeType": "text/html", "headers": [{"name": "Content-Type", "value": 'text/html; charset="iso-8859-1"'}],
                 "body": {"data": b64("<p>Café Labs coding challenge</p>", "iso-8859-1")}}]},
            {"mimeType": "text/plain", "filename": "resume.txt", "body": {"data": b64("ATTACHMENT")}},
        ]}
        text = gmail_watch._body(payload)
        self.assertEqual(text, "Café Labs coding challenge")

    def test_link_text_already_the_url_not_repeated(self):
        text = gmail_watch.html_to_text('<a href="https://x.io/a">https://x.io/a</a>')
        self.assertEqual(text, "https://x.io/a")

    def test_empty_payload(self):
        self.assertEqual(gmail_watch._body({"mimeType": "multipart/mixed", "parts": []}), "")


# ---------------------------------------------------------------- Calendar

class _Req:
    def __init__(self, value):
        self.value = value

    def execute(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeCalendar:
    """Just enough of the Calendar API: calendarList().list() and events().list()."""

    def __init__(self, calendars, pages):
        self.calendars, self.pages, self.calls = calendars, pages, []

    def calendarList(self):
        return self

    def events(self):
        return self

    def list(self, **kw):
        if "calendarId" not in kw:
            return _Req({"items": self.calendars})
        self.calls.append(kw)
        pages = self.pages[kw["calendarId"]]
        if isinstance(pages, Exception):
            return _Req(pages)
        return _Req(pages[int(kw.get("pageToken") or 0)])


def ev(title, start, end, **extra):
    return {"summary": title, "start": {"dateTime": start}, "end": {"dateTime": end}, "status": "confirmed", **extra}


class CalendarRead(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 11, 16, 0, 0, tzinfo=TZ)
        self.end = self.start + timedelta(days=7)
        self.svc = FakeCalendar(
            calendars=[
                {"id": "me@gmail.com", "primary": True, "summary": "me@gmail.com", "selected": True},
                {"id": "classes@group", "summary": "Berkeley classes", "selected": True},
                {"id": "hidden@group", "summary": "Old stuff", "selected": False},
                {"id": "broken@group", "summary": "Broken feed", "selected": True},
            ],
            pages={
                "primary": [
                    {"items": [ev("Club", "2026-11-16T19:00:00-08:00", "2026-11-16T20:00:00-08:00"),
                               {"summary": "Thanksgiving", "start": {"date": "2026-11-26"}, "end": {"date": "2026-11-27"}},
                               ev("Lunch (free)", "2026-11-17T12:00:00-08:00", "2026-11-17T13:00:00-08:00",
                                  transparency="transparent")],
                     "nextPageToken": "1"},
                    {"items": [ev("Declined sync", "2026-11-18T09:00:00-08:00", "2026-11-18T10:00:00-08:00",
                                  attendees=[{"email": "me@gmail.com", "self": True, "responseStatus": "declined"}]),
                               ev("Accepted 1:1", "2026-11-18T11:00:00-08:00", "2026-11-18T11:30:00-08:00",
                                  attendees=[{"email": "me@gmail.com", "self": True, "responseStatus": "accepted"}]),
                               ev("Gone", "2026-11-18T13:00:00-08:00", "2026-11-18T14:00:00-08:00", status="cancelled")]},
                ],
                "classes@group": [{"items": [ev("CS 170 Lecture", "2026-11-16T18:00:00Z", "2026-11-16T19:00:00Z"),
                                             ev("CS 170 Lecture", "2026-11-18T18:00:00Z", "2026-11-18T19:00:00Z")]}],
                "broken@group": RuntimeError("404 calendar gone"),
            },
        )
        self._orig = gcal._service
        gcal._service = lambda: self.svc

    def tearDown(self):
        gcal._service = self._orig

    def test_list_busy(self):
        busy = gcal.list_busy(self.start, self.end)
        self.assertEqual([e["title"] for e in busy], ["CS 170 Lecture", "Club", "CS 170 Lecture", "Accepted 1:1"])
        lecture = busy[0]
        self.assertEqual(lecture["start"], "2026-11-16T10:00:00-08:00")  # UTC converted to local
        self.assertEqual(lecture["calendar"], "Berkeley classes")
        self.assertNotIn("calendar", busy[1])

    def test_queries_expand_recurring_and_page(self):
        gcal.list_busy(self.start, self.end)
        ids = [c["calendarId"] for c in self.svc.calls]
        self.assertEqual(ids, ["primary", "primary", "classes@group", "broken@group"])  # hidden one skipped
        self.assertTrue(all(c["singleEvents"] for c in self.svc.calls))
        self.assertTrue(all(c["timeZone"] == TZ.key for c in self.svc.calls))

    def test_no_calendar_list_permission_falls_back_to_primary(self):
        self.svc.calendarList = lambda: type("X", (), {"list": lambda *a, **k: _Req(RuntimeError("403"))})()
        self.assertEqual(gcal._calendar_ids(self.svc), [("primary", "primary")])


class TokenScopes(unittest.TestCase):
    def tearDown(self):
        config.GOOGLE_TOKEN.unlink(missing_ok=True)
        google_auth._warned.clear()

    def test_old_token_without_new_scope_is_rejected(self):
        config.GOOGLE_TOKEN.write_text(json.dumps({
            "token": "x", "refresh_token": "y", "client_id": "c", "client_secret": "s",
            "scopes": config.GOOGLE_SCOPES[:2]}))
        self.assertIsNone(google_auth.load_credentials())
        self.assertIn("scopes", google_auth._warned)

    def test_corrupt_token(self):
        config.GOOGLE_TOKEN.write_text("{not json")
        self.assertIsNone(google_auth.load_credentials())


# ---------------------------------------------------------------- Pipeline

class Pipeline(unittest.TestCase):
    """Real pipeline + mock calendar; classifier, scheduler and researcher stubbed."""

    def setUp(self):
        gcal.MOCK_FILE.unlink(missing_ok=True)
        self.next_info = None
        self.slot_calls = []
        now = datetime.now(TZ).replace(minute=0, second=0, microsecond=0)
        self.slot_start = now + timedelta(days=1, hours=2)
        self._orig = (classifier.classify_email, scheduler.pick_slot, researcher.write_prep_doc, pipeline.notify.send)
        classifier.classify_email = lambda s, f, b: self.next_info
        pipeline.notify.send = lambda m: None

        def pick_slot(task, duration_minutes, hard_deadline, preference=None):
            start = self.slot_start + timedelta(hours=2 * len(self.slot_calls))
            if hard_deadline and start + timedelta(minutes=duration_minutes) > hard_deadline:
                start = hard_deadline - timedelta(minutes=duration_minutes + 60)
            self.slot_calls.append(hard_deadline)
            return {"start": start, "end": start + timedelta(minutes=duration_minutes), "reasoning": "r", "tip": "t"}

        scheduler.pick_slot = pick_slot
        researcher.write_prep_doc = lambda *a: "# prep"

    def tearDown(self):
        classifier.classify_email, scheduler.pick_slot, researcher.write_prep_doc, pipeline.notify.send = self._orig

    def email(self, **info):
        self.next_info = EmailInfo(summary="s", **info)
        item_id = db.add_item("test", "r@x.com", "subj", "body")
        pipeline._run(item_id, "subj", "r@x.com", "body")  # synchronously
        return db.get_item(item_id)

    def mock_events(self):
        return gcal._mock_created()

    def test_migration_kept_old_rows(self):
        conn = sqlite3.connect(TMP / "oabot.db")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
        conn.close()
        self.assertTrue({"event_id", "duplicate_of"} <= cols)
        self.assertTrue(any(i["subject"] == "old row" for i in db.list_items()))

    def test_oa_reminder_then_earlier_deadline_then_dismiss(self):
        far = (datetime.now(TZ) + timedelta(days=5)).isoformat()
        first = self.email(kind="oa", company="Acme, Inc.", deadline=far, duration_minutes=60)
        self.assertEqual(first["status"], "scheduled")
        self.assertTrue(first["event_id"].startswith("mock-"))
        self.assertEqual([e["id"] for e in self.mock_events()], [first["event_id"]])

        # Reminder, same company spelled differently, no deadline: duplicate, no new block.
        reminder = self.email(kind="oa", company="ACME")
        self.assertEqual(reminder["status"], "duplicate")
        self.assertEqual(reminder["duplicate_of"], first["id"])
        self.assertEqual(reminder["event_start"], first["event_start"])
        self.assertEqual(len(self.mock_events()), 1)
        self.assertEqual(len(self.slot_calls), 1)

        # Reminder whose deadline is BEFORE the current block ends: move the block.
        soon = (gcal.parse(first["event_end"]) - timedelta(hours=1)).isoformat()
        moved = self.email(kind="oa", company="acme", deadline=soon, duration_minutes=60)
        self.assertEqual(moved["status"], "scheduled")
        self.assertEqual(moved["duplicate_of"], first["id"])
        self.assertIn("Moved from", moved["reasoning"])
        self.assertEqual(db.get_item(first["id"])["status"], "replaced")
        self.assertEqual([e["id"] for e in self.mock_events()], [moved["event_id"]])  # old block deleted

        # Dismiss: block gone from the calendar.
        self.assertEqual(pipeline.dismiss(moved["id"]), {"ok": True, "event_deleted": True})
        self.assertEqual(self.mock_events(), [])
        self.assertEqual(db.get_item(moved["id"])["status"], "dismissed")

        # A later reminder must not resurrect a dismissed OA.
        late = self.email(kind="oa", company="Acme", deadline=far)
        self.assertEqual(late["status"], "duplicate")
        self.assertEqual(late["duplicate_of"], moved["id"])
        self.assertEqual(self.mock_events(), [])

    def test_different_and_unknown_companies_are_not_duplicates(self):
        a = self.email(kind="oa", company="Globex")
        b = self.email(kind="oa", company="Initech")
        c = self.email(kind="oa", company=None)
        d = self.email(kind="oa", company=None)
        self.assertEqual([x["status"] for x in (a, b, c, d)], ["scheduled"] * 4)
        self.assertEqual(len(self.mock_events()), 4)

    def test_dismissing_a_duplicate_keeps_the_original_block(self):
        first = self.email(kind="oa", company="Umbrella")
        dup = self.email(kind="oa", company="Umbrella")
        self.assertEqual(pipeline.dismiss(dup["id"])["event_deleted"], False)
        self.assertEqual([e["id"] for e in self.mock_events()], [first["event_id"]])

    def test_dismiss_missing_item(self):
        self.assertEqual(pipeline.dismiss(999999), {"ok": False, "event_deleted": False})

    def test_interview_reminder_is_duplicate_but_new_round_is_not(self):
        when = (datetime.now(TZ) + timedelta(days=3)).replace(hour=14, minute=0, second=0, microsecond=0)
        first = self.email(kind="interview_scheduled", company="Hooli", interview_start=when.isoformat())
        self.assertEqual(first["status"], "prepped")
        again = self.email(kind="interview_scheduled", company="Hooli",
                           interview_start=(when + timedelta(minutes=30)).isoformat())
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["prep_md"], "# prep")
        round2 = self.email(kind="interview_scheduled", company="Hooli",
                            interview_start=(when + timedelta(days=1)).isoformat())
        self.assertEqual(round2["status"], "prepped")
        self.assertEqual(len(self.mock_events()), 2)

    def test_company_key(self):
        self.assertEqual(pipeline.company_key("Stripe, Inc."), pipeline.company_key("stripe"))
        self.assertEqual(pipeline.company_key("Jane Street"), pipeline.company_key("JaneStreet LLC"))
        self.assertNotEqual(pipeline.company_key("Meta"), pipeline.company_key("Metabase"))
        self.assertEqual(pipeline.company_key(None), "")


if __name__ == "__main__":
    unittest.main()
