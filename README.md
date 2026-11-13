# OAbot

**Recruiting emails come in. Time gets blocked and prep gets written. You just show up.**

Built at Hack With Claude (UC Berkeley, Nov 13 2026). Track 01: Agents in Action.

**Demoing? Read [DEMO.md](DEMO.md):** script, checklist, fallbacks, judge Q&A.

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

On the dashboard: one card per email (what it is, the block, the *why*, the prep doc link or
reply draft), and a **This week** strip showing your calendar, coursework deadlines and OAbot's blocks.

Samples: `samples/*_email.txt` are the Sample buttons. `samples/judges/` has one interview email
per hackathon judge; send one with `sh samples/judges/send.sh <name>` (no name lists them).

## Connect the real things

**Google (Gmail + Calendar)** takes about 20 minutes, one person does it:
1. https://console.cloud.google.com -> new project -> enable **Gmail API** and **Google Calendar API**
2. OAuth consent screen: External, Testing. Add each teammate's Gmail under **Test users**
3. Credentials -> Create OAuth client ID -> **Desktop app** -> download as `credentials.json` into this folder
4. `python -m app.google_auth` (browser opens, click through the "unverified app" warning)
5. Restart the server. The pills at the top turn green and the inbox is checked every 15 seconds.
   Only mail that arrives after the server starts is acted on (Promotions and Social are skipped)

**bCourses**: bCourses -> Calendar -> *Calendar Feed* -> copy the link into `.env` as `BCOURSES_ICS_URL`.

**Resume**: save yours as `data/resume.pdf` (or `data/resume.txt`). Without one, `samples/resume.txt` is used.

**Discord pings** (optional): put a channel webhook URL in `.env` as `DISCORD_WEBHOOK_URL`.
Without it, notifications are printed in the server terminal.

## How it works

```
Gmail poll (15s) ─┐
                  ├─> classifier.py ──> oa ─────────> scheduler.py ──> calendar block
Paste box ────────┘   (Claude: what       interview ─> researcher.py ─> prep doc
                       is this email?)                 scheduler.py ──> prep block / reply draft
```

| File | Job | Owner |
|---|---|---|
| `app/google_auth.py`, `app/gmail_watch.py`, `app/gcal.py` | Google login, inbox polling, calendar read/write (mock calendar when not logged in) | johnandrian |
| `app/pipeline.py`, `app/db.py` | Glue: the order things happen in. SQLite store (`data/oabot.db`) | johnandrian (pipeline: everyone, carefully) |
| `app/classifier.py`, `app/scheduler.py`, `app/bcourses.py`, `app/notify.py` | Classify the email, pick the slot, coursework deadlines, console/Discord pings | Shourya2-eng |
| `app/researcher.py` | Web research + prep doc | Victor |
| `app/templates/`, `app/main.py`, `samples/` | Dashboard, API routes, sample emails, demo | Victor |
| `app/config.py`, `app/llm.py` | Settings from `.env`, the shared Claude client | shared |

Prep docs for known interviewers are served from a cache in `samples/prep_cache/` (keyed on
company + first interviewer), so they appear instantly; everyone else is researched live.
You can also run the researcher on its own: `python -m app.researcher --help`.

Where Claude does the work: reading messy emails into structured facts, weighing classes and
exams against the deadline to choose a slot (code then verifies the slot is truly free and falls
back to the earliest free slot if not), live web research on the company and interviewer, writing
prep tied to your actual resume, and drafting the availability reply.

## Demo

Everything for the judging hour is in **[DEMO.md](DEMO.md)**: the 60-second script and a 30-second
cut, the judge-personalised prep doc move, the pre-demo checklist, what to do when something
breaks, and honest answers to the questions judges ask.

## Honest limits

- Never sends email. Availability replies are drafts.
- Runs on a laptop today, for one user; the same code would run on a server around the clock.
- Gmail access is read-only. Calendar access reads your events to find free time and only ever creates new ones.
- Prep docs come from public web pages and can be wrong. The judges' docs were generated ahead of time and cached.
- No rescheduling if you skip a block, and a reminder email about the same OA creates a second block.

Full list in [DEMO.md](DEMO.md#honest-limits).
