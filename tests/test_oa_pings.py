"""Offline tests: the student hears about an OA that could not be blocked, and reminders keep the deadline.
    python -m unittest discover tests -v
"""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402

if "app.db" not in sys.modules:  # run alone: keep the mock calendar and db out of the real data folder
    config.DATA = Path(tempfile.mkdtemp(prefix="oabot-test-pings-"))
    config.GOOGLE_TOKEN = config.DATA / "token.json"

from app import classifier, db, gcal, notify, pipeline, researcher, scheduler, warmup  # noqa: E402
from app.classifier import EmailInfo  # noqa: E402


def _now():
    return datetime.now(config.TZ)


class OaPings(unittest.TestCase):
    def setUp(self):
        gcal.MOCK_FILE.unlink(missing_ok=True)
        self.sent = []
        self._orig = (classifier.classify_email, scheduler.pick_slot, notify.send, warmup.write_plan, config.DEDUPE,
                      researcher.write_prep_doc)
        config.DEDUPE = True
        researcher.write_prep_doc = lambda *a: "# prep"  # never a real Claude call
        notify.send = lambda m: self.sent.append(m)
        warmup.write_plan = lambda *a: {}

        def pick_slot(task, duration_minutes, hard_deadline, preference=None):
            start = _now() + timedelta(hours=1)
            return {"start": start, "end": start + timedelta(minutes=duration_minutes), "reasoning": "r", "tip": "t"}
        scheduler.pick_slot = pick_slot

    def tearDown(self):
        (classifier.classify_email, scheduler.pick_slot, notify.send, warmup.write_plan, config.DEDUPE,
         researcher.write_prep_doc) = self._orig

    def run_email(self, item_id=None, **info):
        classifier.classify_email = lambda s, f, b: EmailInfo(summary="s", **info)
        item_id = item_id or db.add_item("test", "r@x.com", "subj", "body")
        pipeline._run(item_id, "subj", "r@x.com", "body")
        return db.get_item(item_id)

    def fail_slot(self, before=None):
        def pick_slot(*a, **k):
            if before:
                before()
            raise RuntimeError("This needs 105 minutes and is due Fri 5:00 PM, only 50 minutes from now. "
                               "Start it right now, or ask the recruiter for an extension.")
        scheduler.pick_slot = pick_slot

    def test_no_slot_is_urgent_with_link(self):
        self.fail_slot()
        due = _now() + timedelta(minutes=50)
        item = self.run_email(kind="oa", company="Ramp", deadline=due.isoformat(), link="https://hackerrank.com/t/1")
        self.assertEqual(item["status"], "error")
        message = self.sent[-1]
        self.assertIn("Could not block time for the Ramp OA", message)
        self.assertIn("Start it right now", message)
        self.assertIn("Start here: https://hackerrank.com/t/1", message)
        self.assertTrue(notify.is_urgent(message))
        self.assertTrue(notify.format_message(message).startswith("URGENT: "))

    def test_far_deadline_failure_is_not_urgent(self):
        self.fail_slot()
        due = _now() + timedelta(days=3)
        self.run_email(kind="oa", company="Plaid", deadline=due.isoformat())
        self.assertFalse(notify.is_urgent(self.sent[-1]))

    def test_dismissed_meanwhile_stays_quiet(self):
        holder = {"id": db.add_item("test", "r@x.com", "subj", "body")}
        self.fail_slot(before=lambda: db.update_item(holder["id"], status="dismissed"))
        item = self.run_email(holder["id"], kind="oa", company="Brex",
                              deadline=(_now() + timedelta(hours=2)).isoformat())
        self.assertEqual(item["status"], "dismissed")
        self.assertEqual(self.sent, [])

    def test_non_oa_failure_has_no_oa_ping(self):
        self.fail_slot()
        self.run_email(kind="interview_scheduled", company="Pear VC",
                       interview_start=(_now() + timedelta(days=2)).isoformat())
        self.assertFalse(any("OA" in m for m in self.sent))

    def test_reminder_without_deadline_keeps_original(self):
        due = _now() + timedelta(hours=5, minutes=10)
        self.run_email(kind="oa", company="Jane Street", role="SWE Intern", deadline=due.isoformat(),
                       link="https://app.codility.com/test/js1")
        self.sent.clear()
        item = self.run_email(kind="oa", company="Jane Street", link="https://app.codility.com/test/js1")
        self.assertEqual(item["status"], "duplicate")
        message = self.sent[-1]
        self.assertIn("already blocked", message)
        self.assertIn("(due in 5 hours)", message)
        self.assertTrue(notify.is_urgent(message))

    def test_reminder_far_deadline_is_calm(self):
        due = _now() + timedelta(days=4)
        self.run_email(kind="oa", company="Stripe", role="SWE Intern", deadline=due.isoformat(),
                       link="https://hackerrank.com/t/stripe")
        self.sent.clear()
        self.run_email(kind="oa", company="Stripe", link="https://hackerrank.com/t/stripe")
        message = self.sent[-1]
        self.assertIn(", due ", message)
        self.assertFalse(notify.is_urgent(message))


if __name__ == "__main__":
    unittest.main()
