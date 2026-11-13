"""Tell the student what just happened. Console always; Discord if a webhook is set."""
import json
import urllib.request
from datetime import datetime, timedelta
from typing import Optional

from . import config

URGENT_WITHIN = timedelta(hours=24)
_DISCORD_LIMIT = 2000  # characters per message


def time_left(deadline: Optional[datetime], now: Optional[datetime] = None) -> Optional[timedelta]:
    if deadline is None:
        return None
    return deadline - (now or datetime.now(config.TZ))


def is_urgent(deadline: Optional[datetime], now: Optional[datetime] = None) -> bool:
    """Deadline under 24 hours away (or already passed)."""
    left = time_left(deadline, now)
    return left is not None and left < URGENT_WITHIN


def due_in(deadline: datetime, now: Optional[datetime] = None) -> str:
    """'due in 9h 30m', 'due in 45 min', 'deadline passed'."""
    left = time_left(deadline, now)
    minutes = int(left.total_seconds() // 60)
    if minutes <= 0:
        return "deadline passed"
    if minutes < 60:
        return f"due in {minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"due in {hours}h" + (f" {minutes:02d}m" if minutes else "")


def _format(message: str, urgent: bool) -> str:
    message = message.strip()
    if urgent:
        message = f"\U0001F6A8 **URGENT** \U0001F6A8\n{message}"
    return message


def send(message: str, urgent: bool = False):
    """Console always; Discord if DISCORD_WEBHOOK_URL is set. urgent=True shouts and pings @here."""
    text = _format(message, urgent)
    print(f"[OAbot] {text}", flush=True)
    if not config.DISCORD_WEBHOOK_URL:
        return
    # A zero-width space after every '@' in our text defuses any @everyone that came from an email.
    content = ("@here " if urgent else "") + text.replace("@", "@​")
    if len(content) > _DISCORD_LIMIT:
        content = content[:_DISCORD_LIMIT - 1] + "…"
    payload = {
        "content": content,
        # Company names and subjects come from emails: never let them trigger mentions.
        # Only our own @here on urgent messages may ping.
        "allowed_mentions": {"parse": ["everyone"] if urgent else []},
    }
    try:
        req = urllib.request.Request(
            config.DISCORD_WEBHOOK_URL, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "OAbot"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:  # a failed ping must never break the pipeline
        print(f"[OAbot] Discord notify failed: {exc}", flush=True)
