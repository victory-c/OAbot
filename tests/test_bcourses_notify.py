"""Offline tests for the coursework feed and notifications (no Claude, no network)."""
import contextlib
import io
import json
import threading
import unittest
import urllib.error
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from app import bcourses, config, notify

TZ = ZoneInfo("America/Los_Angeles")
FEED_URL = "https://bcourses.berkeley.edu/feeds/calendars/user_x.ics"


def _ev(uid, summary, dtstart):
    return f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20261101T000000Z\r\n{dtstart}\r\nSUMMARY:{summary}\r\nEND:VEVENT\r\n"


def _ics(*events):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Instructure//Canvas//EN\r\n" + "".join(events)
            + "END:VCALENDAR\r\n").encode()


CANVAS = _ics(
    _ev("event-assignment-3", "Project 3 [CS 161]", "DTSTART;TZID=America/New_York:20261119T020000"),  # 11/18 11pm PT
    _ev("event-assignment-1", "Homework 9 [DATA 100]", "DTSTART;VALUE=DATE:20261116"),                  # all day
    _ev("event-assignment-2", "Midterm 2 [CS 170]", "DTSTART:20261118T030000Z"),                        # 11/17 7pm PT
    _ev("event-calendar-event-9", "Lecture [CS 170]", "DTSTART:20261117T180000Z"),
    _ev("event-calendar-event-8", "Office Hours [DATA 100]", "DTSTART;VALUE=DATE:20261117"),
    _ev("event-assignment-4", "Essay [UGIS 157]", "DTSTART;VALUE=DATE:20261230"),                       # outside window
)
START = datetime(2026, 11, 15, 9, 0, tzinfo=TZ)
END = START + timedelta(days=5)


class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status = body, status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class BCoursesTest(unittest.TestCase):
    def setUp(self):
        bcourses.clear_cache()
        self.calls = []
        for p in (mock.patch.object(config, "BCOURSES_ICS_URL", FEED_URL), mock.patch.object(config, "TZ", TZ)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(bcourses.clear_cache)

    def _serve(self, result):
        def fake(req, timeout=None):
            self.calls.append((req.full_url, timeout))
            if isinstance(result, Exception):
                raise result
            return result if isinstance(result, _Resp) else _Resp(result)
        p = mock.patch.object(bcourses.urllib.request, "urlopen", fake)
        p.start()
        self.addCleanup(p.stop)

    def _quiet(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            got = bcourses.upcoming_deadlines(START, END)
        return got, out.getvalue()

    def test_canvas_feed(self):
        self._serve(CANVAS)
        got = bcourses.upcoming_deadlines(START, END)
        self.assertEqual([d["title"] for d in got],
                         ["Homework 9 [DATA 100]", "Midterm 2 [CS 170]", "Project 3 [CS 161]"])  # sorted, no lectures
        self.assertEqual(got[0]["due"], "2026-11-16T23:59:00-08:00")  # all-day -> 11:59 PM local
        self.assertEqual(got[1]["due"], "2026-11-17T19:00:00-08:00")  # UTC -> local
        self.assertEqual(got[2]["due"], "2026-11-18T23:00:00-08:00")  # TZID -> local
        self.assertEqual([bool(d.get("exam")) for d in got], [False, True, False])
        self.assertNotIn("mock", got[0])
        self.assertEqual(got, sorted(got, key=lambda d: d["due"]))
        self.assertLessEqual(self.calls[0][1], 10)  # short timeout

    def test_window_filter(self):
        self._serve(CANVAS)
        got = bcourses.upcoming_deadlines(datetime(2026, 11, 17, 0, 0, tzinfo=TZ), datetime(2026, 11, 17, 23, 0, tzinfo=TZ))
        self.assertEqual([d["title"] for d in got], ["Midterm 2 [CS 170]", "Project 3 [CS 161]"])  # +2 day lookahead
        self.assertEqual(bcourses.upcoming_deadlines(datetime(2027, 3, 1, tzinfo=TZ), datetime(2027, 3, 5, tzinfo=TZ)), [])

    def test_no_canvas_uids_keeps_everything(self):
        self._serve(_ics(_ev("abc@x", "Quiz 4", "DTSTART:20261117T180000Z"),
                         _ev("def@x", "Problem Set 2", "DTSTART;VALUE=DATE:20261116")))
        got = bcourses.upcoming_deadlines(START, END)
        self.assertEqual([d["title"] for d in got], ["Problem Set 2", "Quiz 4"])
        self.assertTrue(got[1].get("exam"))

    def test_exam_words(self):
        for title in ("CS 170 Midterm 2", "Final Exam", "Quiz 3", "MATH 54 Final", "Weekly quizzes"):
            self.assertTrue(bcourses._is_exam(title), title)
        for title in ("Homework 9", "Final Project Report", "Examples worksheet", "Project 3"):
            self.assertFalse(bcourses._is_exam(title), title)

    def test_bad_body(self):
        self._serve(b"<html>Please log in</html>")
        got, out = self._quiet()
        self.assertEqual(got, [])
        self.assertEqual(out.count("[OAbot]"), 1)

    def test_network_error(self):
        self._serve(urllib.error.URLError("timed out"))
        got, out = self._quiet()
        self.assertEqual(got, [])
        self.assertIn("[OAbot]", out)
        self._quiet()
        self.assertEqual(len(self.calls), 1)  # a dead feed is not refetched (and waited on) right away

    def test_non_200(self):
        self._serve(_Resp(CANVAS, status=503))
        self.assertEqual(self._quiet()[0], [])
        bcourses.clear_cache()
        self._serve(urllib.error.HTTPError(FEED_URL, 404, "Not Found", None, None))
        self.assertEqual(self._quiet()[0], [])

    def test_webcal_url(self):
        self._serve(CANVAS)
        with mock.patch.object(config, "BCOURSES_ICS_URL", "webcal://bcourses.berkeley.edu/feeds/x.ics"):
            self.assertEqual(len(bcourses.upcoming_deadlines(START, END)), 3)
        self.assertEqual(self.calls[0][0], "https://bcourses.berkeley.edu/feeds/x.ics")

    def test_cache(self):
        self._serve(CANVAS)
        first = bcourses.upcoming_deadlines(START, END)
        self.assertEqual(bcourses.upcoming_deadlines(START, END), first)
        self.assertEqual(len(self.calls), 1)
        with mock.patch.object(bcourses._time, "monotonic", return_value=bcourses._time.monotonic() + 3600):
            bcourses.upcoming_deadlines(START, END)
        self.assertEqual(len(self.calls), 2)

    def test_mock_when_no_url(self):
        self._serve(urllib.error.URLError("must not be called"))
        with mock.patch.object(config, "BCOURSES_ICS_URL", ""):
            got = bcourses.upcoming_deadlines(START, START + timedelta(days=7))
        self.assertEqual(len(got), 3)
        self.assertTrue(all(d["mock"] for d in got))
        self.assertEqual(got, sorted(got, key=lambda d: d["due"]))
        self.assertEqual(self.calls, [])

    def test_cli(self):
        self._serve(CANVAS)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = bcourses._main(["400"])
        self.assertEqual(code, 0)
        self.assertIn("bcourses.berkeley.edu", out.getvalue())
        self.assertNotIn("user_x", out.getvalue())  # the feed URL is a secret
        self.assertIn("[EXAM] Midterm 2", out.getvalue())


BLOCKED = ("Blocked Sat Nov 14, 7:00 PM for the Stripe OA (due Sun Nov 15, 4:00 AM). "
           "Due in 9 hours, so tonight is the only evening; No classes after 5 PM; CS 170 midterm is Tuesday")


class NotifyTest(unittest.TestCase):
    def test_format_headline_and_reasons(self):
        lines = notify.format_message(BLOCKED).split("\n")
        self.assertEqual(lines[0], "URGENT: Blocked Sat Nov 14, 7:00 PM for the Stripe OA (due Sun Nov 15, 4:00 AM).")
        self.assertEqual(lines[1:], ["- Due in 9 hours, so tonight is the only evening", "- No classes after 5 PM",
                                     "- CS 170 midterm is Tuesday"])

    def test_newline_reasons(self):
        text = notify.format_message("Blocked Thu for the Acme Inc. OA. Free evening\nDue in 3 days")
        self.assertEqual(text, "Blocked Thu for the Acme Inc. OA.\n- Free evening\n- Due in 3 days")

    def test_plain_message_untouched(self):
        msg = "Stripe wants your availability. Reply drafted, review it on the dashboard."
        self.assertEqual(notify.format_message(msg), msg)

    def test_urgency(self):
        for text in ("Due in 9 hours", "due in 23.5 hours", "Due in 45 minutes", "due in under 24 hours", "Due in 1 hour",
                     "Due in 24 hours, so this is the earliest free slot"):  # the scheduler's line at 23.5 to 24 h
            self.assertTrue(notify.is_urgent(text), text)
        for text in ("Due in 30 hours", "Due in 3 days", "Due in 25 hours", "Blocked 7:00 PM, 2 hours long"):
            self.assertFalse(notify.is_urgent(text), text)
        self.assertEqual(notify.format_message("URGENT: due in 2 hours").count("URGENT"), 1)

    def test_cap(self):
        text = notify.format_message("Due in 2 hours. " + "x" * 5000)
        self.assertEqual(len(text), 2000)
        self.assertTrue(text.startswith("URGENT: ") and text.endswith("..."))

    def _send(self, urlopen, message=BLOCKED):
        done = threading.Event()

        def fake(req, timeout=None):
            try:
                return urlopen(req, timeout)
            finally:
                done.set()
        out = io.StringIO()
        with mock.patch.object(config, "DISCORD_WEBHOOK_URL", "https://discord.test/hook"), \
                mock.patch.object(notify.urllib.request, "urlopen", fake), contextlib.redirect_stdout(out):
            self.assertIsNone(notify.send(message))
            self.assertTrue(done.wait(5))
            for t in threading.enumerate():
                if t.name == "oabot-notify":
                    t.join(5)
        return out.getvalue()

    def test_send_posts_formatted(self):
        seen = {}

        def ok(req, timeout):
            seen.update(url=req.full_url, timeout=timeout, body=json.loads(req.data))
            return _Resp(b"")
        out = self._send(ok)
        self.assertIn(f"[OAbot] {BLOCKED}", out)  # console line kept
        self.assertEqual(seen["url"], "https://discord.test/hook")
        self.assertTrue(seen["timeout"])
        self.assertEqual(seen["body"]["content"], notify.format_message(BLOCKED))
        self.assertLessEqual(len(seen["body"]["content"]), 2000)

    def test_webhook_failure_does_not_raise(self):
        def boom(req, timeout):
            raise urllib.error.URLError("discord is down")
        out = self._send(boom)
        self.assertIn("Discord notify failed", out)

    def test_no_webhook_no_network(self):
        out = io.StringIO()
        with mock.patch.object(config, "DISCORD_WEBHOOK_URL", ""), contextlib.redirect_stdout(out), \
                mock.patch.object(notify.urllib.request, "urlopen", side_effect=AssertionError("no network")):
            self.assertIsNone(notify.send("hello"))
            self.assertIsNone(notify.send(None))
        self.assertIn("[OAbot] hello", out.getvalue())


if __name__ == "__main__":
    unittest.main()
