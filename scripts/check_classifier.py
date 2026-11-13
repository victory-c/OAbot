"""Run the real classifier over the sample emails and compare with what we expect.

    .venv/bin/python scripts/check_classifier.py [name-substring ...]

Makes one Claude call per email (needs ANTHROPIC_API_KEY in .env). Time is pinned to NOW so
relative deadlines ("within 72 hours") have one right answer.
"""
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import classifier, config  # noqa: E402

# A Friday afternoon, Pacific standard time.
NOW = datetime(2026, 11, 13, 15, 0, tzinfo=config.TZ)
SENDER = "recruiting@example.com"


def at(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=config.TZ)


# name -> expectations. kind is always checked. deadline: None = must be absent, True = must be
# present, a datetime = must be that exact instant. duration: exact minutes when given.
EXPECTED = {
    "oa_email": {"kind": "oa", "deadline": at(2026, 11, 18, 15, 0), "duration": 90},
    "interview_email": {"kind": "interview_scheduled", "deadline": None},
    "availability_email": {"kind": "interview_needs_availability", "deadline": None},
    "noise_email": {"kind": "other", "deadline": None},
    "tricky/oa_no_deadline": {"kind": "oa", "deadline": None, "duration": 60},
    "tricky/oa_72_hours": {"kind": "oa", "deadline": at(2026, 11, 16, 15, 0), "duration": 75},
    "tricky/hirevue_one_way": {"kind": "oa", "deadline": at(2026, 11, 18, 23, 59), "duration": 30},
    # 11:59 PM ET on Nov 19 is EST (UTC-5) = 8:59 PM PST.
    "tricky/deadline_eastern": {"kind": "oa", "deadline": at(2026, 11, 19, 20, 59), "duration": 70},
    "tricky/reminder_expires_24h": {"kind": "oa", "deadline": at(2026, 11, 14, 15, 0), "duration": 90},
    "tricky/rejection": {"kind": "rejection", "deadline": None},
    "tricky/codesignal_gca": {"kind": "oa", "deadline": at(2026, 11, 20, 17, 0), "duration": 70},
    "tricky/two_part_oa": {"kind": "oa", "deadline": at(2026, 11, 17, 21, 0), "duration": 110},
    "tricky/recruiter_availability": {"kind": "interview_needs_availability", "deadline": None},
    "tricky/job_board_digest": {"kind": "other", "deadline": None},
}


def load(path: Path):
    text = path.read_text()
    first, _, body = text.partition("\n")
    return first.removeprefix("Subject:").strip(), body.strip()


def check(name: str, info) -> list[str]:
    exp = EXPECTED.get(name)
    if exp is None:
        return ["no EXPECTED entry"]
    problems = []
    if info.kind != exp["kind"]:
        problems.append(f"kind {info.kind!r}, expected {exp['kind']!r}")
    want = exp.get("deadline")
    if want is None:
        if info.deadline:
            problems.append(f"deadline {info.deadline}, expected none")
    elif not info.deadline:
        problems.append("deadline missing")
    elif want is not True:
        try:
            got = datetime.fromisoformat(info.deadline)
            if got.tzinfo is None:
                problems.append(f"deadline {info.deadline} has no UTC offset")
            elif got != want:
                problems.append(f"deadline {info.deadline}, expected {want.isoformat()}")
        except ValueError:
            problems.append(f"deadline {info.deadline!r} is not ISO 8601")
    if "duration" in exp and info.duration_minutes != exp["duration"]:
        problems.append(f"duration {info.duration_minutes}, expected {exp['duration']}")
    return problems


def run_one(path: Path):
    name = str(path.relative_to(ROOT / "samples").with_suffix(""))
    subject, body = load(path)
    try:
        info = classifier.classify_email(subject, SENDER, body, now=NOW)
    except Exception as e:  # keep going: one failed call should not hide the rest
        return name, None, [f"error: {e}"]
    return name, info, check(name, info)


def main(argv):
    paths = sorted((ROOT / "samples").glob("*_email.txt")) + sorted((ROOT / "samples" / "tricky").glob("*.txt"))
    if argv:
        paths = [p for p in paths if any(a in str(p) for a in argv)]
    print(f"now = {NOW.isoformat()} ({config.TZ.key}), model = {config.MODEL}\n")
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(run_one, paths))
    ok = 0
    for name, info, problems in results:
        mark = "ok  " if not problems else "FAIL"
        ok += not problems
        if info is None:
            print(f"{mark} {name}")
        else:
            print(f"{mark} {name}: kind={info.kind} company={info.company} deadline={info.deadline} "
                  f"duration={info.duration_minutes} platform={info.platform} link={info.link}")
            print(f"       summary: {info.summary}")
        for p in problems:
            print(f"       MISMATCH: {p}")
    print(f"\n{ok} of {len(results)} correct")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
