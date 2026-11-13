"""The workflow: email in -> classify -> (OA: block time) | (interview: research + prep)."""
import re
import threading
import traceback
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from . import classifier, config, db, gcal, notify, researcher, scheduler

# One scheduling decision at a time: two emails processed together must not both read the
# calendar before either has written its block (double booking), and a reminder must see
# the block its original email just created (duplicate check). Held across Claude calls.
_schedule_lock = threading.Lock()
# Short lock for status changes, never held across Claude calls, so Dismiss answers at once
# and a dismiss can't be overwritten by a pipeline thread finishing a moment later.
_state_lock = threading.Lock()
_claimed_interviews: set[int] = set()  # interview items past the duplicate check, still researching

_SUFFIXES = r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|plc|gmbh|technologies|labs)\b"
_BLOCK_FIELDS = ("event_title", "event_start", "event_end", "event_link", "event_id")


def _why(reasoning):
    """Calendar-description form of the reasoning: one bullet per line."""
    return "Why now:\n" + "\n".join(f"\u2022 {line}" for line in (reasoning or "").splitlines() if line.strip())


def _fmt(dt):
    dt = dt.astimezone(config.TZ)
    return f"{dt:%a %b} {dt.day}, {dt.hour % 12 or 12}:{dt:%M %p}"


def _key(text: str | None) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())
    return re.sub(r"\s+", "", s)


def company_key(name: str | None) -> str:
    """'Stripe, Inc.' and 'stripe' are the same company."""
    return _key(re.sub(_SUFFIXES, " ", (name or "").lower()))


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

        if _is_dismissed(item_id):
            return
        if info.kind == "oa":
            _handle_oa(item_id, info, company)
        elif info.kind in ("interview_scheduled", "interview_needs_availability"):
            _handle_interview(item_id, info, company, role, body)
        else:
            _finish(item_id, status="ignored")
    except Exception as exc:
        traceback.print_exc()
        _finish(item_id, status="error", error=str(exc))
        for dup in db.duplicates_of(item_id):
            db.update_item(dup["id"], status="error",
                           error=f"The earlier email this repeats failed ({exc}). Paste this one again.")
        if isinstance(exc, gcal.CalendarUnavailable):  # nothing was blocked: say so instead of failing silently
            try:
                notify.send("Could not read Google Calendar, so nothing was blocked for a recruiting email. "
                            "Open the dashboard and send it again.")
            except Exception:
                pass
    finally:
        _claimed_interviews.discard(item_id)


# ---------- status writes that respect Dismiss ----------

def _is_dismissed(item_id) -> bool:
    item = db.get_item(item_id)
    return bool(item and item["status"] == "dismissed")


def _finish(item_id, event: dict | None = None, **fields) -> bool:
    """Write the item's result, unless the student dismissed it meanwhile.

    In that case the status and calendar fields are dropped and the event just created
    (if any) is deleted again. Returns False if the item was dismissed.
    """
    if event:
        fields.update(event_id=event["id"], event_link=event["link"])
    with _state_lock:
        if _is_dismissed(item_id):
            keep = {k: v for k, v in fields.items() if k != "status" and k not in _BLOCK_FIELDS}
            db.update_item(item_id, **keep)
            dismissed = True
        else:
            db.update_item(item_id, **fields)
            dismissed = False
    if dismissed and event:
        gcal.delete_event(event["id"])
    return not dismissed


def _delete_block(item) -> bool:
    """Delete an item's calendar block, by id or (rows from before ids were stored) by title + start."""
    if item["event_id"]:
        return gcal.delete_event(item["event_id"])
    if item["event_title"] and item["event_start"]:
        return gcal.delete_event(title=item["event_title"], start=item["event_start"])
    return False


def _copy_result_to_duplicates(original_id):
    """Reminders marked duplicate while the original was still working get its final result."""
    original = db.get_item(original_id)
    for dup in db.duplicates_of(original_id):
        db.update_item(dup["id"], prep_md=original["prep_md"],
                       **{k: original[k] for k in _BLOCK_FIELDS if k != "event_id"})


# ---------- duplicates ----------

_ROLE_NOISE = {"the", "a", "an", "of", "for", "and", "role", "position", "program", "team", "summer", "fall",
               "winter", "spring", "us", "usa", "remote"}
_NOT_INTERN = {"grad", "new", "full", "apprentice", "associate", "senior", "staff"}
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
    if not ta or not tb:
        return True
    if ("intern" in ta) != ("intern" in tb) and (ta | tb) & _NOT_INTERN:
        return False  # an internship and a new-grad / full-time posting are never the same OA
    if ta <= tb or tb <= ta:
        return True
    return len(ta & tb) / len(ta | tb) > 0.5  # strict: 'Quant Trader Intern' is not 'Quant Researcher Intern'


def _link_key(link: str | None) -> str:
    """Same test link despite http/https, 'www.', host case, a trailing slash, a #fragment or utm_* tracking."""
    link = (link or "").strip()
    if not link:
        return ""
    try:
        u = urlsplit(link if "//" in link else "//" + link)
        host = (u.hostname or "").removeprefix("www.")
        query = "&".join(sorted(q for q in u.query.split("&") if q and not q.lower().startswith("utm_")))
        return f"{host}{u.path.rstrip('/')}" + (f"?{query}" if query else "")
    except ValueError:
        return link.rstrip("/")


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
    except (TypeError, ValueError, AttributeError):
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
    rows = [r for r in db.items_of_kind("oa", ("scheduled", "dismissed"), exclude_id=item_id)
            if company_key(r["company"]) == key and _is_current(r, now)]
    same_link = [r for r in rows if link and link == _link_key(r["link"])]

    def same_posting(r):
        other = _link_key(r["link"])
        if link and other and link != other:
            # Two different test links: only a clearly stated, compatible role makes this a reminder.
            # A missing role must not absorb a second-stage or second-posting assessment.
            return bool(_role_tokens(info.role) and _role_tokens(r["role"])) and _same_role(info.role, r["role"])
        return _same_role(info.role, r["role"])

    # A row with the very same test link is the one this email is about; otherwise fall back to role.
    matches = same_link or [r for r in rows if same_posting(r)]
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
    _finish(item_id, status="duplicate", duplicate_of=original["id"], reasoning=why,
            prep_md=original["prep_md"], **{k: original[k] for k in _BLOCK_FIELDS if k != "event_id"})


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
            # Free the old slot first so the scheduler may reuse that time.
            _delete_block(existing)
            try:
                _schedule_oa(item_id, info, company, deadline, replaces=existing)
            except Exception as exc:
                try:
                    _restore_block(existing)
                except Exception as restore_exc:
                    db.update_item(existing["id"], status="error", **dict.fromkeys(_BLOCK_FIELDS),
                                   error=f"Its block was removed to move it, then the move failed ({exc}) and "
                                         f"putting it back failed too ({restore_exc}). Send the email again.")
                raise
            return
        _schedule_oa(item_id, info, company, deadline)


def _restore_block(item):
    """Put a block we deleted for a move back, because the move failed."""
    event = gcal.insert_event(item["event_title"], gcal.parse(item["event_start"]), gcal.parse(item["event_end"]),
                              "\n\n".join(p for p in [f"Start here: {item['link']}" if item["link"] else "",
                                                      _why(item['reasoning']), "Blocked by OAbot."] if p))
    db.update_item(item["id"], event_id=event["id"], event_link=event["link"])


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
                     f"{_fmt(deadline)}, before that block ends\n{reasoning}")
    title = f"OA: {company}" + (f" (due {_fmt(deadline)})" if deadline else "")
    description = "\n\n".join(p for p in [
        f"Start here: {info.link}" if info.link else "",
        f"Prep tip: {slot['tip']}" if slot["tip"] else "",
        _why(reasoning),
        "Blocked by OAbot.",
    ] if p)
    event = gcal.insert_event(title, slot["start"], slot["end"], description)
    if not _finish(item_id, event, status="scheduled", event_title=title, event_start=slot["start"].isoformat(),
                   event_end=slot["end"].isoformat(), reasoning=reasoning,
                   duplicate_of=replaces["id"] if replaces else None):
        if replaces:  # its old block is already gone; the old card must not claim it still exists
            db.update_item(replaces["id"], status="dismissed")
        return  # dismissed while Claude was picking the slot; _finish removed the event
    if replaces:
        with _state_lock:
            old_dismissed = _is_dismissed(replaces["id"])
            if not old_dismissed:
                db.update_item(replaces["id"], status="replaced",
                               reasoning=f"Replaced by a newer email with an earlier deadline (now {_fmt(slot['start'])}).")
        if old_dismissed:  # the student dismissed this OA while we were moving it
            gcal.delete_event(event["id"])
            db.update_item(item_id, status="duplicate", **dict.fromkeys(_BLOCK_FIELDS),
                           reasoning=f"Reminder for the {company} OA you dismissed; nothing scheduled.")
            return
        notify.send(f"{company} OA is due sooner than thought ({_fmt(deadline)}). "
                    f"Moved the block to {_fmt(slot['start'])}.")
        return
    notify.send(f"Blocked {_fmt(slot['start'])} for the {company} OA"
                + (f" (due {_fmt(deadline)})" if deadline else "") + ". " + slot["reasoning"].replace("\n", "; "))


# ---------- interviews ----------

def _handle_interview(item_id, info, company, role, body):
    if info.kind == "interview_scheduled":
        with _schedule_lock:
            existing = _existing_interview(item_id, info)
            if existing:
                _mark_duplicate(item_id, existing, f"Same {company} interview as an earlier email. "
                                                    "Prep doc and prep block already handled.")
                return
            _claimed_interviews.add(item_id)

    # 1. If they asked for availability, draft the reply (shown on the dashboard, never sent).
    if info.kind == "interview_needs_availability":
        draft = scheduler.draft_availability(company, role, info.duration_minutes or 45, body)
        if _finish(item_id, status="needs_reply", draft_reply=draft.reply):
            notify.send(f"{company} wants your availability. Reply drafted, review it on the dashboard.")

    # 2. Research and write the prep doc.
    prep = researcher.write_prep_doc(company, role, info.interviewer_names, info.interview_start or "", body)
    db.update_item(item_id, prep_md=prep)
    _copy_result_to_duplicates(item_id)

    # 3. If the interview time is fixed, block a prep session before it.
    if info.kind == "interview_scheduled" and info.interview_start and not _is_dismissed(item_id):
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
            description = f"Prep doc: http://localhost:8000/prep/{item_id}\n\n{_why(slot['reasoning'])}\n\nBlocked by OAbot."
            event = gcal.insert_event(title, slot["start"], slot["end"], description)
        if _finish(item_id, event, status="prepped", event_title=title, event_start=slot["start"].isoformat(),
                   event_end=slot["end"].isoformat(), reasoning=slot["reasoning"]):
            _copy_result_to_duplicates(item_id)
            notify.send(f"{company} interview on {_fmt(start)}: prep doc ready, prep session blocked {_fmt(slot['start'])}.")
    elif info.kind == "interview_scheduled":
        if _finish(item_id, status="prepped"):
            notify.send(f"{company} interview: prep doc ready.")


# ---------- dismiss ----------

def dismiss(item_id: int) -> dict:
    """Student says 'not doing this' (or 'done'): remove OAbot's calendar block and mark it dismissed.

    Answers immediately, even while other emails are being scheduled. If the item is still
    being processed, the pipeline sees the dismissal and deletes any block it then creates.
    Later reminders for the same OA or interview do not re-block it.
    Returns {"ok", "event_deleted"}; ok is False if the item does not exist.
    """
    with _state_lock:
        item = db.get_item(item_id)
        if not item:
            return {"ok": False, "event_deleted": False}
        db.update_item(item_id, status="dismissed")
    # A duplicate only points at the original's block; deleting it is the original's job.
    if item["status"] in ("duplicate", "replaced", "dismissed"):
        return {"ok": True, "event_deleted": False}
    return {"ok": True, "event_deleted": _delete_block(item)}
