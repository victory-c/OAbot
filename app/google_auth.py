"""Google login. Run once:  python -m app.google_auth

Needs credentials.json (OAuth client, type "Desktop app") in the repo root.
Add each teammate's Gmail as a Test user on the OAuth consent screen.
Run it again whenever config.GOOGLE_SCOPES changes; the server tells you when.
"""
import json
import threading

from . import config

_lock = threading.Lock()
_warned: set[str] = set()


def _warn_once(key: str, message: str):
    if key not in _warned:
        _warned.add(key)
        print(f"[OAbot] {message}", flush=True)


def load_credentials():
    """Return valid Google credentials, or None if nobody has logged in yet (mock mode)."""
    if not config.GOOGLE_TOKEN.exists():
        return None
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    with _lock:  # Gmail thread and request handlers may refresh at the same moment
        try:
            granted = set(json.loads(config.GOOGLE_TOKEN.read_text()).get("scopes") or [])
        except (OSError, ValueError):
            _warn_once("corrupt", "token.json is unreadable. Run: python -m app.google_auth")
            return None
        missing = set(config.GOOGLE_SCOPES) - granted if granted else set()
        if missing:
            _warn_once("scopes", "token.json is missing Google permissions OAbot now needs "
                       f"({', '.join(sorted(s.rsplit('/', 1)[-1] for s in missing))}). "
                       "Using mock mode. Run: python -m app.google_auth")
            return None
        creds = Credentials.from_authorized_user_file(str(config.GOOGLE_TOKEN), config.GOOGLE_SCOPES)
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                _warn_once("refresh", f"Google login expired or was revoked ({exc}). "
                           "Using mock mode. Run: python -m app.google_auth")
                return None
            config.GOOGLE_TOKEN.write_text(creds.to_json())
        return creds if creds.valid else None


def login():
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not config.GOOGLE_CREDENTIALS.exists():
        raise SystemExit("Missing credentials.json. See README -> Google setup.")
    flow = InstalledAppFlow.from_client_secrets_file(str(config.GOOGLE_CREDENTIALS), config.GOOGLE_SCOPES)
    # prompt=consent makes Google return a refresh token every time, so the login survives restarts.
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    config.GOOGLE_TOKEN.write_text(creds.to_json())
    print("Logged in. token.json written.")
    _self_check(creds)


def _self_check(creds):
    """Prove both APIs answer, so problems show up now and not during the demo."""
    from googleapiclient.discovery import build

    try:
        profile = build("gmail", "v1", credentials=creds, cache_discovery=False).users().getProfile(userId="me").execute()
        print(f"  Gmail OK: watching {profile.get('emailAddress')}")
    except Exception as exc:
        print(f"  Gmail FAILED: {exc}\n  -> Is the Gmail API enabled in the Cloud project?")
    try:
        cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
        names = [c.get("summary", c["id"]) for c in cal.calendarList().list().execute().get("items", [])
                 if c.get("primary") or c.get("selected")]
        print(f"  Calendar OK: reading {len(names)} calendar(s): {', '.join(names)}")
    except Exception as exc:
        print(f"  Calendar FAILED: {exc}\n  -> Is the Google Calendar API enabled in the Cloud project?")
    print("Restart the server; the Gmail and Calendar pills should turn green.")
    print("Check your busy time:  python -m app.gcal")


if __name__ == "__main__":
    login()
