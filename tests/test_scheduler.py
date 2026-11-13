"""Offline tests for app/scheduler.py: no Claude, no network, a fixed clock.

Run:  .venv/bin/python -m unittest discover tests
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

from app import config, gcal, scheduler  # noqa: E402

TZ = config.TZ
NOW = datetime(2026, 11, 13, 16, 35, tzinfo=TZ)  # a Friday afternoon, like the demo


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def ev(title, start, end):
    return {"title": title, "start": start.isoformat(), "end": end.isoformat()}


class FakeClaude:
    """Stands in for llm.client(): hands back queued Slot answers (or raises), records every call."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(parsed_output=answer, content=[], stop_reason="end_turn")


def slot(start, reasons=("Clear of every class", "Well before the deadline"), tip="Warm up first"):
    return scheduler.Slot(start=start.isoformat(), reasons=list(reasons), tip=tip)


class SchedulerCase(unittest.TestCase):
    now = NOW
    busy: list = []
    coursework: list = []

    def setUp(self):
        now = self.now

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        for target, value in (
            ("app.scheduler.datetime", Clock),
            ("app.scheduler.gcal.list_busy", lambda s, e: [dict(b) for b in self.busy]),
            ("app.scheduler.bcourses.upcoming_deadlines", lambda s, e: list(self.coursework)),
        ):
            p = mock.patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    def claude(self, *answers):
        fake = FakeClaude(*answers)
        p = mock.patch("app.scheduler.llm.client", lambda: fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def assertLegal(self, result, minutes):
        self.assertEqual(result["end"] - result["start"], timedelta(minutes=minutes))
        s, e = result["start"].astimezone(TZ), result["end"].astimezone(TZ)
        self.assertGreaterEqual(s.hour, config.DAY_START_HOUR)
        self.assertLessEqual((e.hour, e.minute), (config.DAY_END_HOUR, 0))
        self.assertEqual(s.date(), e.date())
        self.assertFalse(scheduler._overlaps(result["start"], result["end"], self.busy))


class ValidTest(unittest.TestCase):
    busy = [ev("CS 170 Lecture", at(16, 10), at(16, 11))]
    win = (at(13, 17), at(18, 23, 59))

    def test_overlap_is_rejected_and_names_the_class(self):
        self.assertIn("CS 170 Lecture", scheduler._valid(at(16, 10, 30), at(16, 12), *self.win, self.busy))

    def test_needs_15_minutes_after_a_class(self):
        self.assertIn("15 minutes", scheduler._valid(at(16, 11, 5), at(16, 12, 5), *self.win, self.busy))
        self.assertIsNone(scheduler._valid(at(16, 11, 15), at(16, 12, 15), *self.win, self.busy))

    def test_day_hours_and_not_past_10pm(self):
        self.assertIsNotNone(scheduler._valid(at(14, 8), at(14, 9, 30), *self.win, []))
        self.assertIsNotNone(scheduler._valid(at(14, 21), at(14, 22, 15), *self.win, []))
        self.assertIsNone(scheduler._valid(at(14, 20, 30), at(14, 22), *self.win, []))

    def test_outside_window(self):
        self.assertIsNotNone(scheduler._valid(at(13, 16, 45), at(13, 17, 45), *self.win, []))

    def test_last_resort_drops_hours_but_never_overlap(self):
        self.assertIsNone(scheduler._valid(at(14, 22), at(14, 23), *self.win, [], strict=False))
        self.assertIsNotNone(scheduler._valid(at(16, 10), at(16, 11), *self.win, self.busy, strict=False))

    def test_first_free_agrees_with_valid(self):
        got = scheduler._first_free(timedelta(minutes=60), at(16, 9, 50), at(16, 22), self.busy)
        self.assertEqual(got, (at(16, 11, 15), at(16, 12, 15)))
        self.assertIsNone(scheduler._valid(*got, at(16, 9, 50), at(16, 22), self.busy))

    def test_soft_rules_exam_and_assignment(self):
        work = [{"title": "CS 170 Midterm 2", "due": at(15, 19).isoformat()},
                {"title": "DATA 100 Homework 9", "due": at(16, 23, 59).isoformat()}]
        self.assertIn("evening before", scheduler._poor(at(14, 18), at(14, 19, 45), NOW, work, True))
        self.assertIn("hours before", scheduler._poor(at(15, 13), at(15, 14, 45), NOW, work, True))
        self.assertIn("right before", scheduler._poor(at(16, 20), at(16, 21, 45), NOW, work, True))
        self.assertIsNone(scheduler._poor(at(14, 11), at(14, 12, 45), NOW, work, True))
        self.assertIn("notice", scheduler._poor(at(13, 17, 30), at(13, 19), NOW, [], True))


class LeadTimeTest(SchedulerCase):
    def test_claude_slot_too_soon_is_rejected_then_retry_is_used(self):
        fake = self.claude(slot(NOW + timedelta(minutes=30)), slot(at(14, 11)))
        got = scheduler.pick_slot("Take the Acme OA", 105, at(18, 23, 59))
        self.assertEqual(got["start"], at(14, 11))
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("does not work", fake.calls[1]["messages"][-1]["content"])
        self.assertLegal(got, 105)

    def test_fallback_keeps_lead_time_when_deadline_is_days_away(self):
        self.claude(RuntimeError("api down"))
        got = scheduler.pick_slot("Take the Acme OA", 105, at(18, 23, 59))
        self.assertGreaterEqual(got["start"], NOW + timedelta(hours=scheduler.SOON_HOURS))
        self.assertLegal(got, 105)

    def test_context_tells_claude_the_earliest_start(self):
        fake = self.claude(slot(at(14, 11)))
        scheduler.pick_slot("Take the Acme OA", 105, at(18, 23, 59))
        self.assertIn("Earliest allowed start: 2026-11-13T17:20", fake.calls[0]["messages"][0]["content"])


class UrgentTest(SchedulerCase):
    busy = [ev("Club meeting", at(13, 17), at(13, 18))]

    def test_takes_earliest_slot_and_says_so_first(self):
        fake = self.claude(slot(at(13, 20), reasons=["Due in 8 hours so go now", "After the club meeting"]))
        got = scheduler.pick_slot("Take the Acme OA", 105, NOW + timedelta(hours=8))
        self.assertEqual(got["start"], at(13, 18, 15))  # 15 min after the club meeting, not Claude's 8 PM
        lines = got["reasoning"].splitlines()
        self.assertEqual(lines[0], "Due in 8 hours, so this is the earliest free slot")
        self.assertEqual(lines[1:], ["After the club meeting"])
        self.assertEqual(got["tip"], "Warm up first")
        self.assertIn("Fixed slot", fake.calls[0]["messages"][0]["content"])
        self.assertLegal(got, 105)

    def test_still_urgent_when_claude_is_down(self):
        self.claude(RuntimeError("api down"))
        got = scheduler.pick_slot("Take the Acme OA", 105, NOW + timedelta(hours=8))
        lines = got["reasoning"].splitlines()
        self.assertTrue(lines[0].startswith("Due in 8 hours"))
        self.assertGreaterEqual(len(lines), 2)

    def test_lead_time_gives_way_when_nothing_later_fits(self):
        self.busy = []
        self.claude(slot(at(13, 16, 45)))
        got = scheduler.pick_slot("Take the Acme OA", 45, NOW + timedelta(minutes=60))
        self.assertEqual(got["start"], at(13, 16, 45))
        self.assertTrue(got["reasoning"].startswith("Due in 60 minutes"))

    def test_too_close_raises_something_actionable(self):
        self.claude()
        with self.assertRaises(RuntimeError) as ctx:
            scheduler.pick_slot("Take the Acme OA", 105, NOW + timedelta(minutes=40))
        msg = str(ctx.exception)
        self.assertIn("105 minutes", msg)
        self.assertIn("5:15 PM", msg)
        self.assertIn("40 minutes", msg)

    def test_calendar_full_before_deadline_raises(self):
        self.busy = [ev("Hackathon", at(13, 9), at(14, 9))]
        self.claude()
        with self.assertRaises(RuntimeError) as ctx:
            scheduler.pick_slot("Take the Acme OA", 105, NOW + timedelta(hours=8))
        self.assertIn("105 minutes", str(ctx.exception))


class LateNightTest(SchedulerCase):
    now = datetime(2026, 11, 13, 22, 30, tzinfo=TZ)

    def test_outside_hours_only_as_last_resort(self):
        self.claude(slot(at(13, 22, 45)))
        got = scheduler.pick_slot("Take the Acme OA", 60, at(14, 1))
        self.assertEqual(got["start"], at(13, 22, 45))
        self.assertIn("outside your usual hours", got["reasoning"].splitlines()[0])


class NoDeadlineTest(SchedulerCase):
    def test_slot_beyond_three_days_is_rejected(self):
        fake = self.claude(slot(at(17, 11)), slot(at(17, 14)))
        got = scheduler.pick_slot("Take the Acme OA", 105, None)
        self.assertEqual(len(fake.calls), 2)
        self.assertLessEqual(got["end"], NOW + timedelta(days=scheduler.NO_DEADLINE_DAYS))
        self.assertLegal(got, 105)
        self.assertGreaterEqual(len(got["reasoning"].splitlines()), 2)

    def test_window_widens_to_five_days_only_when_three_are_full(self):
        self.busy = [ev("Conference trip", at(13, 9), at(16, 22))]
        self.claude(slot(at(17, 11)))
        got = scheduler.pick_slot("Take the Acme OA", 105, None)
        self.assertEqual(got["start"], at(17, 11))

    def test_nothing_free_at_all(self):
        self.busy = [ev("Conference trip", at(13, 9), at(19, 22))]
        self.claude()
        with self.assertRaises(RuntimeError):
            scheduler.pick_slot("Take the Acme OA", 105, None)


class FallbackAndFormatTest(SchedulerCase):
    busy = [ev("CS 170 Lecture", at(16, 10), at(16, 11))]
    coursework = [{"title": "CS 170 Midterm 2", "due": at(15, 19).isoformat()}]

    def test_two_bad_answers_fall_back_to_a_legal_sensible_slot(self):
        fake = self.claude(slot(at(16, 10, 30)), slot(at(14, 7)))
        got = scheduler.pick_slot("Take the Acme OA", 105, at(18, 23, 59))
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("CS 170 Lecture", fake.calls[1]["messages"][-1]["content"])
        self.assertLegal(got, 105)
        self.assertIsNone(scheduler._poor(got["start"], got["end"], NOW, self.coursework, True))
        self.assertEqual(got["tip"], "")

    def test_no_parsed_output_falls_back(self):
        self.claude(None)
        self.assertLegal(scheduler.pick_slot("Take the Acme OA", 105, at(18, 23, 59)), 105)

    def test_reasoning_is_plain_lines(self):
        self.claude(slot(at(14, 11), reasons=["- Clear of Sunday's CS 170 Midterm 2.", "• Free morning — no classes",
                                              "* Four days before the deadline", "", "five", "six"]))
        got = scheduler.pick_slot("Take the Acme OA", 105, at(18, 23, 59))
        lines = got["reasoning"].splitlines()
        self.assertTrue(2 <= len(lines) <= 4)
        for line in lines:
            self.assertFalse(line[0] in "-*• ", line)
            self.assertFalse(line.endswith("."), line)
            self.assertNotIn("—", line)
        self.assertEqual(lines[0], "Clear of Sunday's CS 170 Midterm 2")

    def test_calendar_unavailable_propagates(self):
        self.claude(slot(at(14, 11)))
        with mock.patch("app.scheduler.gcal.list_busy", side_effect=gcal.CalendarUnavailable("down")):
            with self.assertRaises(gcal.CalendarUnavailable):
                scheduler.pick_slot("Take the Acme OA", 105, at(18, 23, 59))

    def test_past_deadline(self):
        self.claude()
        with self.assertRaises(RuntimeError):
            scheduler.pick_slot("Interview prep", 60, NOW - timedelta(hours=1))


if __name__ == "__main__":
    unittest.main()
