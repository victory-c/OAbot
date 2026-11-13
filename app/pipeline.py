"""The workflow: email in -> classify -> (OA: block time) | (interview: research + prep)."""
import threading
import traceback
from datetime import timedelta

from . import classifier, config, db, gcal, notify, researcher, scheduler


def _fmt(dt):
    dt = dt.astimezone(config.TZ)
    return f"{dt:%a %b} {dt.day}, {dt.hour % 12 or 12}:{dt:%M %p}"


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


def _handle_oa(item_id, info, company):
    minutes = (info.duration_minutes or 90) + 15  # setup + buffer
    deadline = gcal.parse(info.deadline) if info.deadline else None
    platform = f" on {info.platform}" if info.platform else ""
    slot = scheduler.pick_slot(
        task=f"Take the {company} online assessment{platform} ({info.duration_minutes or 90} min test)",
        duration_minutes=minutes, hard_deadline=deadline,
    )
    title = f"OA: {company}" + (f" (due {_fmt(deadline)})" if deadline else "")
    description = "\n\n".join(p for p in [
        f"Start here: {info.link}" if info.link else "",
        f"Prep tip: {slot['tip']}" if slot["tip"] else "",
        f"Why now: {slot['reasoning']}",
        "Blocked by OAbot.",
    ] if p)
    link = gcal.create_event(title, slot["start"], slot["end"], description)
    db.update_item(item_id, status="scheduled", event_title=title, event_start=slot["start"].isoformat(),
                   event_end=slot["end"].isoformat(), event_link=link, reasoning=slot["reasoning"])
    notify.send(f"Blocked {_fmt(slot['start'])} for the {company} OA"
                + (f" (due {_fmt(deadline)})" if deadline else "") + f". {slot['reasoning']}")


def _handle_interview(item_id, info, company, role, body):
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
        slot = scheduler.pick_slot(
            task=f"Interview prep for {company} ({role}): read the prep doc, rehearse stories out loud",
            duration_minutes=60, hard_deadline=start - timedelta(hours=2),
        )
        title = f"Prep: {company} interview"
        description = f"Prep doc: http://localhost:8000/prep/{item_id}\n\nWhy now: {slot['reasoning']}\n\nBlocked by OAbot."
        link = gcal.create_event(title, slot["start"], slot["end"], description)
        db.update_item(item_id, status="prepped", event_title=title, event_start=slot["start"].isoformat(),
                       event_end=slot["end"].isoformat(), event_link=link, reasoning=slot["reasoning"])
        notify.send(f"{company} interview on {_fmt(start)}: prep doc ready, prep session blocked {_fmt(slot['start'])}.")
    elif info.kind == "interview_scheduled":
        db.update_item(item_id, status="prepped")
        notify.send(f"{company} interview: prep doc ready.")
