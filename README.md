# OAbot

**Recruiting emails come in. Time gets blocked and prep gets written. You just show up.**

Built at Hack With Claude (UC Berkeley, Nov 13 2026). Track 01: Agents in Action.

Students miss online assessments (OAs) and walk into interviews unprepared, not because they
can't do them but because deciding *when* costs mental energy they don't have. OAbot watches
your inbox and does that part for you:

| Email arrives | What OAbot does |
|---|---|
| **OA invite** | Reads the deadline, looks at your calendar and bCourses workload, blocks the best slot (with the test link and a prep tip inside the event) |
| **Interview confirmed** | Researches the company and interviewer on the web, reads your resume, writes a prep doc, blocks a prep session before the interview |
| **"Send your availability"** | Picks 3 times that fit your week and drafts the reply (you send it) |
| Anything else | Ignored |

## Run it (2 minutes, no Google needed)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then put your ANTHROPIC_API_KEY in .env
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000, click a **Sample** button, hit **Process**.

Without Google login it runs in **mock mode**: a fake class schedule and fake bCourses
deadlines, and blocks are saved locally. Everything Claude does is real.

## Connect the real things

**Google (Gmail + Calendar)** takes about 20 minutes, one person does it:
1. https://console.cloud.google.com -> new project -> enable **Gmail API** and **Google Calendar API**
2. OAuth consent screen: External, Testing. Add each teammate's Gmail under **Test users**
3. Credentials -> Create OAuth client ID -> **Desktop app** -> download as `credentials.json` into this folder
4. `python -m app.google_auth` (browser opens, click through the "unverified app" warning)
5. Restart the server. The pills at the top turn green and the inbox is checked every 15 seconds

**bCourses**: bCourses -> Calendar -> *Calendar Feed* -> copy the link into `.env` as `BCOURSES_ICS_URL`.

**Resume**: save yours as `data/resume.pdf` (or `data/resume.txt`).

**Discord pings** (optional): put a channel webhook URL in `.env` as `DISCORD_WEBHOOK_URL`.

## How it works

```
Gmail poll (15s) ─┐
                  ├─> classifier.py ──> oa ─────────> scheduler.py ──> calendar block
Paste box ────────┘   (Claude: what       interview ─> researcher.py ─> prep doc
                       is this email?)                 scheduler.py ──> prep block / reply draft
```

| File | Job | Owner |
|---|---|---|
| `app/gmail_watch.py`, `app/google_auth.py`, `app/gcal.py` | Google login, inbox polling, calendar read/write | |
| `app/classifier.py`, `app/scheduler.py`, `app/bcourses.py` | Classify the email, pick the slot | |
| `app/researcher.py` | Web research + prep doc | |
| `app/templates/`, `app/main.py`, `samples/` | Dashboard, demo script | |
| `app/pipeline.py` | Glue: the order things happen in | everyone, carefully |

Where Claude does the work: reading messy emails into structured facts, weighing classes and
exams against the deadline to choose a slot (code then verifies the slot is truly free), live web
research on the company and interviewer, and writing prep tied to your actual resume.

## Demo script (60 seconds)

1. "I missed two OAs this semester. Not because they were hard. Because I never decided when to do them."
2. Teammate sends the OA email. ~20 seconds later the block appears on the calendar. Read the *why* line out loud.
3. Send the interview email naming the judge in front of you. Open the prep doc about them.
4. "It never sends email for you and it shows its reasoning for every block."

## Honest limits

- Never sends email. Availability replies are drafts.
- Runs on a laptop today; the same code would run on a server around the clock.
- Gmail access is read-only. Calendar access only creates events.
