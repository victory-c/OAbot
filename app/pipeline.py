"""The workflow: email in -> classify -> (OA: block time) | (interview: research + prep)."""
import re
import threading
import traceback
from datetime import datetime, timedelta

from . import classifier, config, db, gcal, notify, researcher, scheduler

# One scheduling decision at a time: two emails processed together must not both read the
# calendar before either has written its block (double booking), and a reminder must see
# the block its original email just created (duplicate check).
_schedule_lock = threading.Lock()
_claimed_interviews: set[int] = set()  # interview items past the duplicate check, still researching

_SUFFIXES = r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|plc|gmbh|technologies|labs)\b"


def _fmt(dt):
    dt = dt.astimezone(config.TZ)
    return f"{dt:%a %b} {dt.day}, {dt.hour % 12 or 12}:{dt:%M %p}"


def company_key(name: str | None) -> str:
    """'Stripe, Inc.' and 'stripe' are the same company."""
    s = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    s = re.sub(_SUFFIXES, " ", s)
    return re.sub(r"\s+", "", s)


def submit(subject: str, sender: str, body: str, source: str = "paste") -> int:
    """Queue an email. Returns the item id immediately; work happens in the background."""
    item_id = db.add_item(source, sender, subject, body)
    threading.Thread(target=_run, args=(item_id, subject, sender, body), daemon=True).start()
    return item_id


def _run(item_id, subject, sender, body):
    try:
        info = classifier.classify_email(subject, sender, body)
        db.update_item(item_id, kind=info.kind, company=info.company, role=info.role,
                       deadline=info.deadline, link=info.link, info_json=info.model_dump())
        company = info.company or "Unknown company"
        role = info.role or "the role"

        if info.kind == "oa":
            _handle_oa(item_id, info, company)
        elif info.kind in ("interview_scheduled", "interview_needs_availability"):
            _handle_interview(item_id, info, company, role, body)
        else:
            db.update_item(item_id, status="ignored")
    except Exception as exc:
        traceback.print_exc()
        db.update_item(item_id, status="error", error=str(exc))
    finally:
        _claimed_interviews.discard(item_id)


# ---------- duplicates ----------

_ROLE_NOISE = {"the", "a", "an", "of", "for", "and", "role", "position", "program", "team", "summer", "fall",
               "winter", "spring", "us", "usa", "remote"}
_STALE_AFTER = timedelta(days=14)  # how long a dismissed OA with no stated deadline keeps absorbing reminders


def _role_tokens(role: str | None) -> set[str]:
    """'Software Engineering Intern (Summer 2027)' -> {software, engineer, intern}."""
    out = set()
    for w in re.findall(r"[a-z]+", (role or "").lower()):
        if w in _ROLE_NOISE:
            continue
        if w.startswith("intern"):
            w = "intern"
        elif w in ("graduate", "grads", "graduates"):
            w = "grad"
        elif w.endswith("ing") and len(w) > 5:
            w = w[:-3]
        elif w.endswith("s") and len(w) > 3:
            w = w[:-1]
        out.add(w)
    return out


def _same_role(a: str | None, b: str | None) -> bool:
    """Same posting, allowing for how differently two emails word it. A reminder often names no
    role at all, so a missing role matches anything; 'intern' vs 'new grad' does not match."""
    ta, tb = _role_tokens(a), _role_tokens(b)
    if not ta or not tb or ta <= tb or tb <= ta:
        return True
    return len(ta & tb) / len(ta | tb) >= 0.5


def _link_key(link: str | None) -> str:
    return (link or "").strip().rstrip("/")


def _is_current(row: dict, now: datetime) -> bool:
    """Is this earlier OA still a live commitment? A finished block, a passed deadline or last
    season's OA must not swallow a new email: missing an OA is worse than a second block."""
    try:
        if row["deadline"] and gcal.parse(row["deadline"]) < now:
            return False
        if row["status"] == "scheduled":  # the block is still ahead of us
            return bool(row["event_end"]) and gcal.parse(row["event_end"]) > now
        # dismissed: honour it until its deadline, or for a while if it never had one
        return bool(row["deadline"]) or now - gcal.parse(row["created"]) <= _STALE_AFTER
    except (TypeError, ValueError):
        return False  # unreadable dates: do not let this row block anything


def _existing_oa(item_id, info):
    """An earlier email about the SAME OA that is already handled and still current, or None.

    Same OA = same company, the earlier one is still live (see _is_current), and either the
    same test link or a compatible role. Company alone is not enough: a new-grad OA is not a
    reminder for last month's internship OA."""
    key = company_key(info.company)
    if not key or not config.DEDUPE:
        return None  # "Unknown company" must never match another unknown company
    now = datetime.now(config.TZ)
    link = _link_key(info.link)
    matches = [r for r in db.items_of_kind("oa", ("scheduled", "dismissed"), exclude_id=item_id)
               if company_key(r["company"]) == key and _is_current(r, now)
               and ((link and link == _link_key(r["link"])) or _same_role(info.role, r["role"]))]
    # A live block beats a dismissed one; among those, the newest.
    matches.sort(key=lambda r: (r["status"] == "scheduled", r["id"]))
    return matches[-1] if matches else None


def _existing_interview(item_id, info):
    """An earlier email confirming the same interview (same company, start within an hour)."""
    key = company_key(info.company)
    if not key or not info.interview_start or not config.DEDUPE:
        return None
    start = gcal.parse(info.interview_start)
    for r in db.items_of_kind("interview_scheduled", ("processing", "prepped", "dismissed"), exclude_id=item_id):
        if company_key(r["company"]) != key:
            continue
        if r["status"] == "processing" and r["id"] not in _claimed_interviews:
            continue  # not past its own duplicate check yet; it will see us instead
        other = (classifier.EmailInfo.model_validate_json(r["info_json"]).interview_start
                 if r["info_json"] else None)
        if other and abs(gcal.parse(other) - start) <= timedelta(hours=1):
            return r
    return None


def _mark_duplicate(item_id, original, why):
    db.update_item(item_id, status="duplicate", duplicate_of=original["id"], reasoning=why,
                   event_title=original["event_title"], event_start=original["event_start"],
                   event_end=original["event_end"], event_link=original["event_link"])


# ---------- OA ----------

def _handle_oa(item_id, info, company):
    deadline = gcal.parse(info.deadline) if info.deadline else None
    with _schedule_lock:
        existing = _existing_oa(item_id, info)
        if existing:
            block_end = gcal.parse(existing["event_end"]) if existing["event_end"] else None
            if existing["status"] == "dismissed":
                _mark_duplicate(item_id, existing, f"Reminder for the {company} OA you dismissed; nothing scheduled.")
                return
            if not (deadline and block_end and block_end > deadline):
                when = f" for {_fmt(gcal.parse(existing['event_start']))}" if existing["event_start"] else ""
                _mark_duplicate(item_id, existing, f"Reminder for the {company} OA. Already blocked{when}, "
                                                    "which still beats the deadline. No new block.")
                notify.send(f"Reminder from {company}: your OA is already blocked{when}.")
                return
            # The reminder shows an earlier deadline than the current block allows: move it.
        _schedule_oa(item_id, info, company, deadline, replaces=existing)


def _schedule_oa(item_id, info, company, deadline, replaces=None):
    minutes = (info.duration_minutes or 90) + 15  # setup + buffer
    platform = f" on {info.platform}" if info.platform else ""
    slot = scheduler.pick_slot(
        task=f"Take the {company} online assessment{platform} ({info.duration_minutes or 90} min test)",
        duration_minutes=minutes, hard_deadline=deadline,
    )
    reasoning = slot["reasoning"]
    if replaces:
        reasoning = (f"Moved from {_fmt(gcal.parse(replaces['event_start']))}: a newer email says it's due "
                     f"{_fmt(deadline)}, before that block ends. {reasoning}")
    title = f"OA: {company}" + (f" (due {_fmt(deadline)})" if deadline else "")
    description = "\n\n".join(p for p in [
        f"Start here: {info.link}" if info.link else "",
        f"Prep tip: {slot['tip']}" if slot["tip"] else "",
        f"Why now: {reasoning}",
        "Blocked by OAbot.",
    ] if p)
    event = gcal.insert_event(title, slot["start"], slot["end"], description)
    db.update_item(item_id, status="scheduled", event_title=title, event_start=slot["start"].isoformat(),
                   event_end=slot["end"].isoformat(), event_link=event["link"], event_id=event["id"],
                   reasoning=reasoning, duplicate_of=replaces["id"] if replaces else None)
    if replaces:  # only after the new block exists, so a failure never leaves the OA unblocked
        gcal.delete_event(replaces["event_id"])
        db.update_item(replaces["id"], status="replaced",
                       reasoning=f"Replaced by a newer email with an earlier deadline (now {_fmt(slot['start'])}).")
        notify.send(f"{company} OA is due sooner than thought ({_fmt(deadline)}). "
                    f"Moved the block to {_fmt(slot['start'])}.")
        return
    notify.send(f"Blocked {_fmt(slot['start'])} for the {company} OA"
                + (f" (due {_fmt(deadline)})" if deadline else "") + f". {slot['reasoning']}")


# ---------- interviews ----------

def _handle_interview(item_id, info, company, role, body):
    if info.kind == "interview_scheduled":
        with _schedule_lock:
            existing = _existing_interview(item_id, info)
            if existing:
                if existing["prep_md"]:
                    db.update_item(item_id, prep_md=existing["prep_md"])
                _mark_duplicate(item_id, existing, f"Same {company} interview as an earlier email. "
                                                    "Prep doc and prep block already handled.")
                return
            _claimed_interviews.add(item_id)

    # 1. If they asked for availability, draft the reply (shown on the dashboard, never sent).
    if info.kind == "interview_needs_availability":
        draft = scheduler.draft_availability(company, role, info.duration_minutes or 45, body)
        db.update_item(item_id, status="needs_reply", draft_reply=draft.reply)
        notify.send(f"{company} wants your availability. Reply drafted, review it on the dashboard.")

    # 2. Research and write the prep doc.
    prep = researcher.write_prep_doc(company, role, info.interviewer_names, info.interview_start or "", body)
    db.update_item(item_id, prep_md=prep)

    # 3. If the interview time is fixed, block a prep session before it.
    if info.kind == "interview_scheduled" and info.interview_start:
        start = gcal.parse(info.interview_start)
        with _schedule_lock:
            slot = scheduler.pick_slot(
                task=(f"Interview prep for {company} ({role}): read the prep doc, rehearse stories out loud. "
                      f"The interview itself starts {_fmt(start)}; the hard deadline below is 2 hours before it"),
                duration_minutes=60, hard_deadline=start - timedelta(hours=2),
                preference=("As close to the interview as is sensible, so it stays fresh: ideally the afternoon or "
                            "evening of the day before, otherwise the morning of the interview."),
            )
            title = f"Prep: {company} interview"
            description = f"Prep doc: http://localhost:8000/prep/{item_id}\n\nWhy now: {slot['reasoning']}\n\nBlocked by OAbot."
            event = gcal.insert_event(title, slot["start"], slot["end"], description)
        db.update_item(item_id, status="prepped", event_title=title, event_start=slot["start"].isoformat(),
                       event_end=slot["end"].isoformat(), event_link=event["link"], event_id=event["id"],
                       reasoning=slot["reasoning"])
        notify.send(f"{company} interview on {_fmt(start)}: prep doc ready, prep session blocked {_fmt(slot['start'])}.")
    elif info.kind == "interview_scheduled":
        db.update_item(item_id, status="prepped")
        notify.send(f"{company} interview: prep doc ready.")


# ---------- dismiss ----------

def dismiss(item_id: int) -> dict:
    """Student says 'not doing this' (or 'done'): remove OAbot's calendar block and mark it dismissed.

    Later reminders for the same OA or interview are then recognised and do not re-block it.
    Returns {"ok", "event_deleted"}; ok is False if the item does not exist.
    """
    item = db.get_item(item_id)
    if not item:
        return {"ok": False, "event_deleted": False}
    deleted = False
    with _schedule_lock:
        # A duplicate only points at the original's block; deleting it is the original's job.
        if item["event_id"] and item["status"] not in ("duplicate", "replaced"):
            deleted = gcal.delete_event(item["event_id"])
        db.update_item(item_id, status="dismissed")
    return {"ok": True, "event_deleted": deleted}
