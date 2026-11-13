"""Offline tests for the OA warm-up plan and the Quiz me transcript checks.
    python -m unittest discover tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import quiz, warmup  # noqa: E402


class DetectPlatform(unittest.TestCase):
    def test_link_host_wins(self):
        self.assertEqual(warmup.detect_platform("HackerRank", "https://app.codesignal.com/x", ""), "CodeSignal")
        self.assertEqual(warmup.detect_platform(None, "www.hackerrank.com/tests/abc", ""), "HackerRank")

    def test_stated_then_body(self):
        self.assertEqual(warmup.detect_platform("hirevue video interview", None, ""), "HireVue")
        self.assertEqual(warmup.detect_platform(None, "https://jobs.acme.com/oa", "via Codility today"), "Codility")

    def test_lookalike_host_is_not_the_platform(self):
        self.assertIsNone(warmup.detect_platform(None, "https://nothackerrank.com.evil.io/x", ""))

    def test_unknown_kept_as_read(self):
        self.assertEqual(warmup.detect_platform("Mettl", None, ""), "Mettl")
        self.assertIsNone(warmup.detect_platform(None, None, "Please complete the assessment."))


class WarmupFormat(unittest.TestCase):
    PLAN = warmup.Warmup(
        kind="coding", from_email=["2 questions, 70 minutes"], typical=["Hidden test cases — often"],
        steps=[warmup.Step(minutes=10, task="- Two easy array problems"), warmup.Step(minutes=20, task="BFS refresher")],
        before_start=["Close Slack"])

    def test_markdown(self):
        md = warmup.to_markdown(self.PLAN, "Ramp", "CodeSignal", 70)
        self.assertTrue(md.startswith("# Ramp: OA warm-up\n"))
        self.assertIn("## What the email says\n- 2 questions, 70 minutes", md)
        self.assertIn("## 30-minute warm-up", md)
        self.assertIn("1. **10 min.** Two easy array problems", md)
        self.assertNotIn("—", md)

    def test_calendar(self):
        self.assertEqual(warmup.to_calendar(self.PLAN),
                         "Warm-up:\n• 10 min: Two easy array problems\n• 20 min: BFS refresher")


class QuizTranscript(unittest.TestCase):
    def test_empty_starts(self):
        self.assertEqual(quiz.check([]), [])

    def test_alternates_and_ends_on_student(self):
        ok = [{"role": "assistant", "content": "Q1"}, {"role": "user", "content": " my answer "}]
        self.assertEqual(quiz.check(ok)[-1], {"role": "user", "content": "my answer"})
        for bad in ([{"role": "user", "content": "hi"}],
                    [{"role": "assistant", "content": "Q1"}],
                    [{"role": "assistant", "content": "Q1"}, {"role": "assistant", "content": "x"}],
                    [{"role": "assistant", "content": "Q1"}, {"role": "user", "content": "  "}],
                    [{"role": "system", "content": "x"}]):
            with self.assertRaises(quiz.BadTranscript):
                quiz.check(bad)

    def test_limits(self):
        with self.assertRaises(quiz.BadTranscript):
            quiz.check([{"role": "assistant", "content": "Q"}, {"role": "user", "content": "x" * 5000}])
        long = [{"role": "assistant" if i % 2 == 0 else "user", "content": "x"} for i in range(quiz.MAX_TURNS + 2)]
        with self.assertRaises(quiz.BadTranscript):
            quiz.check(long)

    def test_mode_prompts(self):
        self.assertIn("online assessment", quiz.system_prompt("oa", "# doc"))
        self.assertIn("mock interview", quiz.system_prompt("interview_scheduled", "# doc"))
        self.assertIn("<prep_doc>\n# doc\n</prep_doc>", quiz.system_prompt("oa", "# doc"))


if __name__ == "__main__":
    unittest.main()
