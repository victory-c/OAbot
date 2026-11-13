"""Offline tests for issue #2 (scheduling brain): urgent notifications, Discord payloads,
the bCourses feed, slot notice / fallback rules, and the pipeline's urgent pings.
    python -m unittest discover tests -v
"""
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402

if "app.db" not in sys.modules:  # run alone: keep the mock calendar and db out of the real data folder
    config.DATA = Path(tempfile.mkdtemp(prefix="oabot-test-brain-"))
    config.GOOGLE_TOKEN = config.DATA / "token.json"

from app import bcourses, classifier, db, gcal, notify, pipeline, scheduler, warmup  # noqa: E402
from app.classifier import EmailInfo  # noqa: E402

TZ = config.TZ


def _now():
    return datetime.now(TZ)


# ---------------------------------------------------------------- notify

class Urgency(unittest.TestCase):
    def test_is_urgent(self):
        now = datetime(2026, 11, 13, 12, 0, tzinfo=TZ)
        self.assertTrue(notify.is_urgent(now + timedelta(hours=23, minutes=59), now))
        self.assertTrue(notify.is_urgent(now - timedelta(hours=1), now))  # already passed: still shout
        self.assertFalse(notify.is_urgent(now + timedelta(hours=24), now))
        self.assertFalse(notify.is_urgent(None, now))

    def test_due_in(self):
        now = datetime(2026, 11, 13, 12, 0, tzinfo=TZ)
        self.assertEqual(notify.due_in(now + timedelta(minutes=45), now), "due in 45 min")
        self.assertEqual(notify.due_in(now + timedelta(hours=9), now), "due in 9h")
        self.assertEqual(notify.due_in(now + timedelta(hours=9, minutes=5), now), "due in 9h 05m")
        self.assertEqual(notify.due_in(now - timedelta(minutes=1), now), "deadline passed")


class Discord(unittest.TestCase):
    def setUp(self):
        self._url = config.DISCORD_WEBHOOK_URL
        config.DISCORD_WEBHOOK_URL = "https://discord.example/webhook"
        self.sent = []

        def fake_urlopen(req, timeout=None):
            self.sent.append(json.loads(req.data))
        self._patch = mock.patch.object(notify.urllib.request, "urlopen", fake_urlopen)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        config.DISCORD_WEBHOOK_URL = self._url

    def test_plain_message_pings_nobody(self):
        notify.send("Blocked Thu 7:00 PM for the @everyone OA")
        payload = self.sent[0]
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertNotIn("@everyone", payload["content"])  # defused with a zero-width space
        self.assertNotIn("URGENT", payload["content"])

    def test_urgent_is_loud(self):
        notify.send("Citadel OA: blocked Fri 1:00 PM", urgent=True)
        payload = self.sent[0]
        self.assertTrue(payload["content"].startswith("@here "))
        self.assertIn("URGENT", payload["content"])
        self.assertEqual(payload["allowed_mentions"], {"parse": ["everyone"]})
        self.assertEqual(payload["content"].count("@here"), 1)

    def test_long_message_fits_discord(self):
        notify.send("x" * 5000, urgent=True)
        self.assertLessEqual(len(self.sent[0]["content"]), 2000)

    def test_failure_never_raises(self):
        with mock.patch.object(notify.urllib.request, "urlopen", side_effect=OSError("down")):
            notify.send("hello")  # no exception

    def test_no_webhook_no_request(self):
        config.DISCORD_WEBHOOK_URL = ""
        notify.send("hello", urgent=True)
        self.assertEqual(self.sent, [])


# ---------------------------------------------------------------- bCourses

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Instructure//Canvas//EN
BEGIN:VEVENT
UID:a1
DTSTART:20261116T075900Z
SUMMARY:Homework 9 [DATA C100]
END:VEVENT
BEGIN:VEVENT
UID:a2
DTSTART;VALUE=DATE:20261118
SUMMARY:Midterm 2 [CS 170]
END:VEVENT
BEGIN:VEVENT
UID:a3
DTSTART:20261201T120000
SUMMARY:Project 3 [CS 161]
END:VEVENT
BEGIN:VEVENT
UID:a4
SUMMARY:No date at all
END:VEVENT
END:VCALENDAR
"""


class _Resp:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class BCourses(unittest.TestCase):
    def setUp(self):
        self._url = config.BCOURSES_ICS_URL
        config.BCOURSES_ICS_URL = "webcal://bcourses.berkeley.edu/feeds/calendars/user_abc.ics"
        bcourses._cache.update(url=None, at=0.0, events=None)
        self.fetched = []

        def fake_urlopen(req, timeout=None):
            self.fetched.append(req.full_url)
            return _Resp(ICS)
        self._patch = mock.patch.object(bcourses.urllib.request, "urlopen", fake_urlopen)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        config.BCOURSES_ICS_URL = self._url
        bcourses._cache.update(url=None, at=0.0, events=None)

    def test_parse_feed(self):
        events = bcourses.parse_feed(ICS)
        self.assertEqual([e["title"] for e in events],
                         ["Homework 9 [DATA C100]", "Midterm 2 [CS 170]", "Project 3 [CS 161]"])
        hw = datetime.fromisoformat(events[0]["due"])
        self.assertEqual((hw.month, hw.day, hw.hour, hw.minute), (11, 15, 23, 59))  # 07:59Z = 11:59 PM Pacific
        mid = datetime.fromisoformat(events[1]["due"])
        self.assertEqual((mid.day, mid.hour, mid.minute), (18, 23, 59))  # all-day -> end of that day
        proj = datetime.fromisoformat(events[2]["due"])
        self.assertEqual(proj.hour, 12)  # floating time read as the student's

    def test_webcal_becomes_https_and_window_filter(self):
        start = datetime(2026, 11, 14, tzinfo=TZ)
        got = bcourses.upcoming_deadlines(start, start + timedelta(days=3))
        self.assertTrue(self.fetched[0].startswith("https://bcourses.berkeley.edu/"))
        self.assertEqual([e["title"] for e in got], ["Homework 9 [DATA C100]", "Midterm 2 [CS 170]"])

    def test_cached_between_calls(self):
        start = datetime(2026, 11, 14, tzinfo=TZ)
        bcourses.upcoming_deadlines(start, start + timedelta(days=3))
        bcourses.upcoming_deadlines(start, start + timedelta(days=30))
        self.assertEqual(len(self.fetched), 1)

    def test_outage_uses_last_copy_or_raises(self):
        start = datetime(2026, 11, 14, tzinfo=TZ)
        with mock.patch.object(bcourses.urllib.request, "urlopen", side_effect=OSError("down")):
            with self.assertRaises(OSError):
                bcourses.upcoming_deadlines(start, start + timedelta(days=3))
        bcourses.upcoming_deadlines(start, start + timedelta(days=3))  # good copy cached
        bcourses._cache["at"] = 0.0  # expire it
        with mock.patch.object(bcourses.urllib.request, "urlopen", side_effect=OSError("down")):
            got = bcourses.upcoming_deadlines(start, start + timedelta(days=3))
        self.assertEqual(len(got), 2)

    def test_mock_without_feed(self):
        config.BCOURSES_ICS_URL = ""
        start = _now()
        got = bcourses.upcoming_deadlines(start, start + timedelta(days=5))
        self.assertTrue(got and all(e.get("mock") for e in got))
        self.assertEqual(self.fetched, [])


# ---------------------------------------------------------------- scheduler

def _fake_client(slots):
    """A Claude stand-in whose messages.parse returns the given Slot objects in turn."""
    calls = []

    def parse(**kwargs):
        calls.append(kwargs)
        slot = slots[min(len(calls) - 1, len(slots) - 1)]
        return SimpleNamespace(parsed_output=slot, content=[], stop_reason="end_turn")
    return SimpleNamespace(messages=SimpleNamespace(parse=parse)), calls


class Scheduler(unittest.TestCase):
    def setUp(self):
        self._busy = gcal.list_busy
        gcal.list_busy = lambda s, e: []
        self._url = config.BCOURSES_ICS_URL
        config.BCOURSES_ICS_URL = ""

    def tearDown(self):
        gcal.list_busy = self._busy
        config.BCOURSES_ICS_URL = self._url

    def test_notice(self):
        now = datetime(2026, 11, 13, 12, 0, tzinfo=TZ)
        self.assertEqual(scheduler.earliest_start(now, None), now + timedelta(hours=2))
        self.assertEqual(scheduler.earliest_start(now, now + timedelta(days=3)), now + timedelta(hours=2))
        self.assertEqual(scheduler.earliest_start(now, now + timedelta(hours=10)), now + timedelta(minutes=15))

    def test_first_free_never_before_window_start(self):
        ws = datetime(2026, 11, 13, 12, 7, 30, tzinfo=TZ)
        start, end = scheduler._first_free(timedelta(minutes=60), ws, ws + timedelta(days=1), [])
        self.assertEqual((start.hour, start.minute), (12, 30))
        ws = datetime(2026, 11, 13, 12, 30, tzinfo=TZ)
        start, _ = scheduler._first_free(timedelta(minutes=60), ws, ws + timedelta(days=1), [])
        self.assertEqual((start.hour, start.minute), (12, 30))

    def test_too_close_raises_noslot(self):
        with self.assertRaises(scheduler.NoSlot):
            scheduler.pick_slot("Take the OA", 105, _now() + timedelta(minutes=60))
        self.assertTrue(issubclass(scheduler.NoSlot, RuntimeError))

    def test_slot_too_soon_is_rejected_then_falls_back(self):
        soon = (_now() + timedelta(minutes=30)).replace(second=0, microsecond=0)
        bad = scheduler.Slot(start=soon.isoformat(), end=(soon + timedelta(minutes=60)).isoformat(),
                             reasons=["r"], tip="t")
        client, calls = _fake_client([bad])
        with mock.patch.object(scheduler.llm, "client", lambda: client):
            got = scheduler.pick_slot("Take the OA", 60, _now() + timedelta(days=3))
        self.assertEqual(len(calls), 2)  # one retry with the reason
        self.assertIn("slot must be inside", calls[1]["messages"][-1]["content"])
        self.assertGreaterEqual(got["start"], _now() + timedelta(hours=2) - timedelta(minutes=1))
        self.assertEqual(got["reasoning"], "Earliest free slot before the deadline.")

    def test_prompt_mentions_notice_and_coursework_outage(self):
        good_start = (_now() + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
        ok = scheduler.Slot(start=good_start.isoformat(), end=(good_start + timedelta(minutes=60)).isoformat(),
                            reasons=["- Clear afternoon."], tip="t")
        client, calls = _fake_client([ok])
        config.BCOURSES_ICS_URL = "https://bcourses.example/feed.ics"
        bcourses._cache.update(url=None, at=0.0, events=None)
        try:
            with mock.patch.object(scheduler.llm, "client", lambda: client), \
                    mock.patch.object(bcourses.urllib.request, "urlopen", side_effect=OSError("down")):
                got = scheduler.pick_slot("Take the OA", 60, _now() + timedelta(days=3))
        finally:
            bcourses._cache.update(url=None, at=0.0, events=None)
        prompt = calls[0]["messages"][0]["content"]
        self.assertIn("Earliest allowed start:", prompt)
        self.assertIn("bCourses feed unavailable", prompt)
        self.assertEqual(got["start"], good_start)
        self.assertEqual(got["reasoning"], "Clear afternoon")


class ClassifierPrompt(unittest.TestCase):
    def test_tricky_cases_covered(self):
        for phrase in ("HireVue", "rejection", "expires in 24 hours", "Eastern", "leave deadline null"):
            self.assertIn(phrase, classifier.SYSTEM)


class Samples(unittest.TestCase):
    def test_sample_format(self):
        for path in (config.ROOT / "samples").glob("*_email.txt"):
            subject, sep, body = path.read_text().partition("\n\n")
            self.assertTrue(subject.startswith("Subject: ") and sep and body.strip(), path.name)


# ---------------------------------------------------------------- pipeline pings

class UrgentPings(unittest.TestCase):
    def setUp(self):
        gcal.MOCK_FILE.unlink(missing_ok=True)
        self.sent = []
        self._orig = (classifier.classify_email, scheduler.pick_slot, notify.send, warmup.write_plan, config.DEDUPE)
        config.DEDUPE = False
        notify.send = lambda m, urgent=False: self.sent.append((m, urgent))
        warmup.write_plan = lambda *a: {}

        def pick_slot(task, duration_minutes, hard_deadline, preference=None):
            start = _now() + timedelta(hours=1)
            return {"start": start, "end": start + timedelta(minutes=duration_minutes),
                    "reasoning": "Free Friday afternoon\nClear of CS 170 Midterm 2", "tip": "t"}
        scheduler.pick_slot = pick_slot

    def tearDown(self):
        (classifier.classify_email, scheduler.pick_slot, notify.send, warmup.write_plan, config.DEDUPE) = self._orig

    def run_email(self, **info):
        classifier.classify_email = lambda s, f, b: EmailInfo(summary="s", **info)
        item_id = db.add_item("test", "r@x.com", "subj", "body")
        pipeline._run(item_id, "subj", "r@x.com", "body")
        return db.get_item(item_id)

    def test_urgent_oa_is_loud(self):
        due = _now() + timedelta(hours=10)
        item = self.run_email(kind="oa", company="Citadel", deadline=due.isoformat(), duration_minutes=90)
        self.assertEqual(item["status"], "scheduled")
        message, urgent = self.sent[-1]
        self.assertTrue(urgent)
        self.assertIn("Citadel OA: blocked", message)
        self.assertIn("due in 9h", message)
        self.assertIn("• Free Friday afternoon\n• Clear of CS 170 Midterm 2", message)

    def test_relaxed_oa_is_quiet(self):
        due = _now() + timedelta(days=4)
        self.run_email(kind="oa", company="Stripe", deadline=due.isoformat(), duration_minutes=90)
        message, urgent = self.sent[-1]
        self.assertFalse(urgent)
        self.assertNotIn("due in", message)

    def test_no_deadline(self):
        self.run_email(kind="oa", company="Datadog", duration_minutes=70)
        message, urgent = self.sent[-1]
        self.assertFalse(urgent)
        self.assertIn("No deadline given", message)

    def test_no_slot_says_do_it_now(self):
        def no_slot(*a, **k):
            raise scheduler.NoSlot("The deadline is too close to fit this in. Do it now.")
        scheduler.pick_slot = no_slot
        due = _now() + timedelta(minutes=50)
        item = self.run_email(kind="oa", company="Ramp", deadline=due.isoformat(), link="https://hackerrank.com/t/1")
        self.assertEqual(item["status"], "error")
        message, urgent = self.sent[-1]
        self.assertTrue(urgent)
        self.assertIn("Ramp OA: no time left", message)
        self.assertIn("Start: https://hackerrank.com/t/1", message)

    def test_no_slot_after_dismiss_stays_quiet(self):
        holder = {}

        def no_slot(*a, **k):
            db.update_item(holder["id"], status="dismissed")  # student dismissed while Claude was thinking
            raise scheduler.NoSlot("No free slot before the deadline. Do it now.")
        scheduler.pick_slot = no_slot
        classifier.classify_email = lambda s, f, b: EmailInfo(
            summary="s", kind="oa", company="Ramp", deadline=(_now() + timedelta(hours=2)).isoformat())
        holder["id"] = db.add_item("test", "r@x.com", "subj", "body")
        pipeline._run(holder["id"], "subj", "r@x.com", "body")
        self.assertEqual(db.get_item(holder["id"])["status"], "dismissed")
        self.assertEqual(self.sent, [])

    def test_reminder_without_deadline_uses_original(self):
        config.DEDUPE = True
        due = _now() + timedelta(hours=5)
        self.run_email(kind="oa", company="Jane Street", role="SWE Intern", deadline=due.isoformat(),
                       link="https://app.codility.com/test/js1")
        self.sent.clear()
        item = self.run_email(kind="oa", company="Jane Street", link="https://app.codility.com/test/js1")
        self.assertEqual(item["status"], "duplicate")
        message, urgent = self.sent[-1]
        self.assertTrue(urgent)
        self.assertIn("due in 4h", message)
        self.assertNotIn("No deadline given", message)

    def test_rejection_sends_nothing(self):
        item = self.run_email(kind="rejection", company="Figma")
        self.assertEqual(item["status"], "ignored")
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
