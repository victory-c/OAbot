"""Tell the student what just happened. Console always; Discord if a webhook is set."""
import json
import urllib.request

from . import config


def send(message: str):
    print(f"[OAbot] {message}", flush=True)
    if not config.DISCORD_WEBHOOK_URL:
        return
    try:
        req = urllib.request.Request(
            config.DISCORD_WEBHOOK_URL, data=json.dumps({"content": message}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "OAbot"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:  # a failed ping must never break the pipeline
        print(f"[OAbot] Discord notify failed: {exc}", flush=True)
