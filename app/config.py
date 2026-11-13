import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)

DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

MODEL = os.getenv("OABOT_MODEL", "claude-opus-5-5")
# Explicit so a proxy ANTHROPIC_BASE_URL in someone's shell doesn't hijack the app.
API_BASE_URL = os.getenv("OABOT_BASE_URL", "https://api.anthropic.com")
TZ = ZoneInfo(os.getenv("OABOT_TZ", "America/Los_Angeles"))

BCOURSES_ICS_URL = os.getenv("BCOURSES_ICS_URL", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

POLL_SECONDS = int(os.getenv("OABOT_POLL_SECONDS", "15"))
GMAIL_QUERY = os.getenv("OABOT_GMAIL_QUERY", "newer_than:1d -category:promotions -category:social")

GOOGLE_CREDENTIALS = ROOT / "credentials.json"   # downloaded from Google Cloud console
GOOGLE_TOKEN = ROOT / "token.json"               # written by `python -m app.google_auth`
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events",    # create / delete OAbot's blocks
    "https://www.googleapis.com/auth/calendar.readonly",  # read busy time on every calendar (classes)
]

# Treat a second email about the same OA or interview as a reminder (no new block).
# Set OABOT_DEDUPE=0 to schedule every email, e.g. to re-run the same sample in a demo.
DEDUPE = os.getenv("OABOT_DEDUPE", "1").strip().lower() not in ("0", "false", "no", "off")

# Scheduling preferences
DAY_START_HOUR = 9
DAY_END_HOUR = 22
