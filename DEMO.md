# OAbot demo sheet

Judging 4:30 to 5:30 PM. Code freeze 4:15 PM. Live demo only, no slides.
One person talks. One person drives the laptop. One person holds the phone.

Jump to: [60s script](#60-second-script) | [30s cut](#30-second-cut) | [judge move](#the-judge-move) |
[checklist](#pre-demo-checklist) | [fallbacks](#if-something-breaks) | [Q&A](#questions-judges-will-ask) |
[rubric](#how-each-rubric-line-is-earned) | [limits](#honest-limits)

---

## 60-second script

Dashboard: http://localhost:8000

**0:00 Problem (one breath, Victor)**

> "I keep missing online assessments, or doing them at the last minute, or walking into
> interviews without prepping. Not because they are hard. Because I am busy and I never
> decide WHEN to do them. OAbot decides for me."

**0:10 OA email goes in**

- Gmail pill green: phone person sends the saved **Stripe OA** draft to the connected inbox.
  Say: "A recruiter just emailed me."
- Gmail pill grey: click **Sample: oa**, then **Process**.

A card appears with a spinner. It took about 13 seconds in our test run (time it in rehearsal).
Talk over the wait:

> "Claude is reading the email for the deadline, then looking at my classes and my
> coursework deadlines to pick a slot. Code then checks the slot is really free."

**0:30 Block appears**

- Card turns **scheduled**. Point at **Blocked for you** and read the **Why this slot** text out loud.
- Point at the block in the **This week** strip.
- If Google is connected: click **open in Calendar**. It is on the real calendar.

**0:40 The judge move** (details in the next section)

- Ask the judge their name. Send their interview email.
- Card appears. Click **Open prep doc**. Scroll to **Your interviewer(s)**. The numbered links go to the sources.

> "This is the prep doc I would read 20 minutes before interviewing with you."

**0:55 Close**

> "It never sends email. Every block shows its reasoning. I just show up."

---

## 30-second cut

Use when the judge is rushed or a queue is forming. Skip the OA wait.

1. "I miss OAs and skip interview prep because I never decide when to do them. OAbot decides for me." (5s)
2. Point at the **Stripe OA card already on the dashboard** from your warm-up run.
   "This email came in, it blocked this slot, and here is why." Read the *why* line. (10s)
3. Send the judge's email. Open the prep doc. "And this is my prep doc for interviewing with you." (12s)
4. "It never sends email. I just show up." (3s)

---

## The judge move

Each judge has a ready-made interview email in `samples/judges/`.

| Judge at the table | Company | File / name to use |
|---|---|---|
| Jay Patel | VoiceBit | `voicebit-jay-patel` |
| Ajay Kamat | Pear VC | `pear-vc-ajay-kamat` |
| Ashtan Jordan | Precursor Ventures | `precursor-ventures-ashtan-jordan` |
| Ye Wang | EverCurrent | `evercurrent-ye-wang` |
| Ayush Malhotra | Talkhappi | `talkhappi-ayush-malhotra` |

**When to trigger it:** right after the OA block lands (0:40). Not before. The OA proves the
agent acts. The prep doc is the moment they remember.

**Three ways to send it. Pick one in rehearsal and stick to it.**

1. **Phone (best, needs Gmail green).** Phone person sends that judge's saved draft to the
   connected inbox. It shows up within about 15 seconds.
2. **Terminal (fastest).** In the repo folder:
   ```
   sh samples/judges/send.sh voicebit-jay-patel
   ```
   Run `sh samples/judges/send.sh` with no name to list all five.
3. **Paste box.** Open `samples/judges/<name>.txt`, copy all of it, paste into the dashboard
   box, click **Process**. The "Subject:" line can stay in the body.

**What happens:** Claude reads the email (a few seconds). The prep doc link shows up on the
card. A prep session gets blocked on the calendar a little later (about 10 more seconds).
Do not wait for the block. Open the doc as soon as the link appears.

**Say this, honestly, before they ask:**

> "We researched the five judges earlier today, so yours loads from a cache. For anyone
> else it runs the same web research live, which takes about twenty seconds."

If they want proof it is live: click **Sample: availability**, then **Process**. It is a made-up
Ramp email, so nothing is cached: the card shows a drafted reply after about 10 seconds and a
live-researched prep doc after about 25 (our test run). The interviewer in it is invented, so the
doc says it could not identify her. Keep talking while it runs.

**Careful:**

- The prep doc was written by Claude from public web pages. It can be wrong about a person.
  Every company and person fact carries a numbered link, and the doc ends with a **Sources** list.
  If the judge spots an error, say "good catch, that is why it is a starting point, not a script" and move on.
- If the doc says it could not confidently identify the interviewer, that is the designed
  behaviour. Say so: "it refuses to guess rather than mix you up with someone else."
- Do not edit the company or interviewer name in the judge emails. The cache is looked up by them.

---

## Pre-demo checklist

Do this at 4:00 PM. Tick each one.

**Server**

- [ ] Terminal open in the repo. Server running: `uvicorn app.main:app --reload --port 8000`
- [ ] http://localhost:8000 loads. Pills at the top show what is connected.
- [ ] `.env` has `ANTHROPIC_API_KEY`. Check without printing it: `grep -c ANTHROPIC_API_KEY=sk .env` prints `1`.
- [ ] Laptop on power. Sleep and screen lock off. Notifications off.

**Resume**

- [ ] `ls data/resume.pdf` shows Victor's resume. (No file means it uses the fake `samples/resume.txt`.)

**Prep cache**

- [ ] `ls samples/prep_cache` shows five `.md` files, one per judge name in the table above.
- [ ] If one is missing, that judge's doc will be researched live (about 20 seconds, and nobody has checked it). Tell Victor now.

**Warm-up run (this is also your safety net)**

- [ ] Clear out cards and blocks left over from testing: stop the server, delete `data/oabot.db` and
      `data/mock_calendar.json`, start the server again. (Old test blocks sit in the past and clutter the strip.)
- [ ] Click **Sample: oa**, **Process**. Wait for **scheduled**. Note how many seconds it took.
- [ ] Send all five judge emails once:
      `for j in voicebit-jay-patel pear-vc-ajay-kamat precursor-ventures-ashtan-jordan evercurrent-ye-wang talkhappi-ayush-malhotra; do sh samples/judges/send.sh $j; sleep 30; done`
      (about 3 minutes; the pause stops two prep blocks landing on the same slot)
- [ ] Every card ends **scheduled** or **prepped**. No red **error** cards. Open each prep doc once and skim it for anything embarrassing.
- [ ] Leave these cards on the dashboard. If anything breaks later, you demo from them.
- [ ] Sending a judge's email again during the demo just adds a second card for them. That is fine.
- [ ] If Google Calendar is connected, the warm-up puts real blocks on it. Leave them or delete them by hand.

**Tabs open, in this order**

- [ ] 1. OAbot dashboard
- [ ] 2. Google Calendar, week view (only if the Calendar pill says google)
- [ ] 3. This file

**Phone (only if the Gmail pill is green)**

- [ ] Six drafts saved, addressed to the connected inbox: the Stripe OA (`samples/oa_email.txt`) and the five judge emails.
- [ ] Send one test email now. It must appear on the dashboard within about 30 seconds.
- [ ] The watcher only reacts to mail that arrives AFTER the server started. If the server restarts, wait 20 seconds before sending.

**Roles**

- [ ] Who talks, who drives, who holds the phone. Who answers Gmail/Calendar questions (johnandrian), classifier/scheduling questions (Shourya2-eng), prep doc questions (Victor).

---

## If something breaks

Stay calm, keep talking, switch. Never debug in front of a judge.

**Gmail login broken, or the Gmail pill is grey**

- Use the paste box or `sh samples/judges/send.sh <name>`. Everything after the inbox is identical.
- Say: "Live it watches Gmail. For the demo I will paste the same email in."
- Calendar pill says mock: blocks still appear on the card and in the **This week** strip, against a
  sample class schedule. Say that it is a sample schedule.

**Email sent from the phone never shows up**

- Wait 30 seconds, once. Then use the paste box. Do not resend three times.
- Likely causes: Gmail filed it under Promotions or Social (those are skipped), or the server restarted.

**Wifi down**

- First: phone hotspot. The app only needs to reach the Claude API (and Google, if connected).
- No internet at all: nothing NEW can be processed. Every step starts with Claude reading the email.
- Still works offline: the dashboard, every card from the warm-up run, every prep doc (they are
  stored on the laptop), the **This week** strip in mock mode.
- Demo from the warm-up cards. Say: "Wifi is down, so here is the run from twenty minutes ago."

**Claude is slow, or a card sits on "working"**

- Say: "Claude is weighing my class schedule against the deadline right now. While it thinks,
  here is one it finished earlier." Then show a warm-up card and its *why* line.
- Come back to the live card if it finishes. Point it out: "and there is the live one."
- Past 60 seconds for an OA: stop waiting, move to the judge move.

**Red error card**

- "The deadline is too close to fit this in. Do it now." means the email's deadline is nearly here. That is the app being honest, not a crash. Use a different sample.
- Anything else: do not read it out. Scroll to a good card. Check the server terminal after the judge leaves.

**Dashboard will not load**

- Look at the server terminal. If it died: `source .venv/bin/activate && uvicorn app.main:app --reload --port 8000`
- Cards and prep docs survive a restart. They are saved in `data/oabot.db`.

---

## Questions judges will ask

Keep answers to two sentences. Say "we have not built that" when it is true.

**Why not just paste the email into ChatGPT?**
Then I still have to notice the email, open a chat, and decide when to do it. That is the exact
step I keep skipping. OAbot is not a chat: the email arrives and the block is on my calendar
before I have opened anything.

**How is this different from Simplify or Huntr?**
As we understand them, those help you apply to jobs and track applications. OAbot starts after
that: the recruiter has replied and now there is a deadline or an interview to act on.

**How is this different from Motion?**
Motion, as we understand it, schedules tasks you give it. OAbot finds the task for you in your
inbox, and it knows what an OA is: a timed test, one sitting, a hard deadline.

**Gmail already adds events to my calendar.**
Gmail can put an event on the calendar when an email has a set time. An OA has a deadline, not
a time, so somebody still has to choose when. OAbot chooses, around classes and coursework, and
tells you why. Gmail also does not write interview prep.

**You are reading my whole inbox?**
Gmail access is read-only. It cannot send, delete or change mail. It checks recent mail (last
day, skipping Promotions and Social), and each of those is sent to Claude to decide whether it
is recruiting mail. Non-recruiting mail is marked ignored and nothing more happens. Items are
stored in a database on this laptop. Today that is one user on their own machine.

**What can it do to my calendar?**
It reads your events to find free time and it creates new blocks. The code never edits or
deletes an existing event.

**What if an email tries to trick it?**
Emails are passed to Claude as untrusted data, with instructions not to follow anything inside
them. And the agent has no way to send email, so the worst case is a wrong calendar block or a
wrong prep doc.

**What does Claude actually do, versus plain code?**
Claude does four things:
1. Reads a messy email into facts: what kind it is, company, role, deadline, link, interviewer.
2. Picks the slot, weighing classes, exams and assignments, and explains the choice.
3. Searches the web for the company and the interviewer, reads my resume, writes the prep doc.
4. Drafts the reply when a recruiter asks for availability.

Plain code does the rest: polling Gmail, reading and writing the calendar, storing results, and
checking Claude's slot. If the slot overlaps something or is outside 9 AM to 10 PM, code rejects
it, asks once more, then falls back to the earliest free slot.

**Why does it need an agent? Is this not just a classifier?**
The classifier is step one. After it, the work branches: block time, or research and write, or
draft a reply, and each branch ends in something done, not a suggestion.

**Is the research about me accurate? Where did it come from?**
Public professional web pages only, found by Claude's web search today. Each fact links to its
source and the doc ends with a Sources list. It says so when it cannot confidently identify
someone. It can still be wrong.

**Who pays?**
Nobody yet. It runs on our own Claude API key. The likely buyer is the student, a few dollars a
month during recruiting season, or a career center buying it for students. We have not tested
either.

**How many users?**
The team. We built it today.

**What is next?**
1. Host it so it runs around the clock, not on a laptop.
2. Notice when a block was missed and reschedule it.
3. Recognise follow-ups and reminders about the same OA so they update the block instead of adding one.
4. Send the availability reply with one tap of approval.

---

## How each rubric line is earned

Each line is scored 1 to 5. Track fit adds up to 2.

| Rubric line | What earns it | Where in the demo |
|---|---|---|
| **Impact** | A real problem for a clear group: students in recruiting season who miss OAs and skip prep. Victor's own story. | 0:00 problem line. Do not skip it. |
| **Use of Claude** | Four jobs, none of them a chat: extract, choose a time, research, write. Web search and resume reading in one doc. | The *why* line on the card. The prep doc. The "Claude vs code" answer. |
| **Technical execution** | Email in, calendar block out, end to end, live. Code verifies Claude's slot. | 0:10 to 0:40. Real Gmail and real Calendar if the pills are green. |
| **Demo and storytelling** | One-breath problem, one crisp flow, limits said out loud before being asked. | Saying "yours is cached" unprompted. The close line. |
| **Creativity** | The prep doc is about the judge standing at the table. | The judge move. |
| **Track fit: Agents in Action** | It finishes the task: the block is on the calendar and the doc is written. Nobody typed a prompt. | "I just show up." |

---

## Honest limits

Say these out loud if asked. Offer one or two unprompted.

- Runs on one laptop, for one user. Not hosted.
- Never sends email. Availability replies are drafts you copy and send yourself.
- Checks Gmail every 15 seconds. It skips Promotions and Social, and only acts on mail that arrives after it starts.
- The Google app is in testing mode, so only accounts we added as test users can log in.
- Without Google login it uses a sample class schedule. Without a bCourses link it uses sample deadlines.
- The judges' prep docs were generated earlier today and are served from a cache. Anyone else is researched live, about 20 seconds.
- Prep docs come from public web pages and can be wrong or out of date. It will not guess when it cannot identify someone.
- It does not notice if you skip a block, and it does not reschedule.
- Two emails about the same OA (an invite and a reminder) create two blocks.
- It reads the plain-text part of an email. Heavily formatted emails may come through thin.
- Times assume Pacific time. All-day calendar events are ignored when finding free time.
- The link to the prep doc inside the calendar event only opens on this laptop.
- We have no accuracy numbers for the classifier. It was built and tested today on a handful of emails.
