"""Google login. Run once:  python -m app.google_auth

Needs credentials.json (OAuth client, type "Desktop app") in the repo root.
Add each teammate's Gmail as a Test user on the OAuth consent screen.
Run it again whenever config.GOOGLE_SCOPES changes; the server tells you when.
"""
import json
import os
import threading

from . import config

# Without this one OAbot still works: it just reads busy time from the primary calendar only.
OPTIONAL_SCOPES = {"https://www.googleapis.com/auth/calendar.readonly"}

_lock = threading.Lock()
_warned: set[str] = set()
_cache: dict = {"mtime": None, "creds": None}  # token.json is read again only when it changes


def _warn_once(key: str, message: str):
    if key not in _warned:
        _warned.add(key)
        print(f"[OAbot] {message}", flush=True)


def _short(scopes) -> str:
    return ", ".join(sorted(s.rsplit("/", 1)[-1] for s in scopes))


def _read_token():
    from google.oauth2.credentials import Credentials

    try:
        info = json.loads(config.GOOGLE_TOKEN.read_text())
    except (OSError, ValueError):
        _warn_once("corrupt", "token.json is unreadable. Run: python -m app.google_auth")
        return None
    granted = set(info.get("scopes") or [])
    if granted:
        missing = set(config.GOOGLE_SCOPES) - granted
        if missing - OPTIONAL_SCOPES:
            _warn_once("scopes", f"token.json is missing Google permissions OAbot needs ({_short(missing)}). "
                       "Using mock mode. Run: python -m app.google_auth")
            return None
        if missing:
            _warn_once("optional", f"Google login lacks {_short(missing)}: only the primary calendar is "
                       "checked for busy time. Run python -m app.google_auth to grant it.")
    # Load with the scopes the token really has, so a refresh never asks for more than was granted.
    return Credentials.from_authorized_user_info(info, sorted(granted) or config.GOOGLE_SCOPES)


def load_credentials():
    """Return valid Google credentials, or None if nobody has logged in yet (mock mode)."""
    try:
        mtime = config.GOOGLE_TOKEN.stat().st_mtime
    except FileNotFoundError:
        return None
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request

    with _lock:  # Gmail thread and request handlers may refresh at the same moment
        if _cache["mtime"] != mtime:
            _cache["creds"], _cache["mtime"] = _read_token(), mtime
        creds = _cache["creds"]
        if creds is None:
            return None
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                _warn_once("refresh", f"Google login expired or was revoked ({exc}). "
                           "Using mock mode. Run: python -m app.google_auth")
                return None
            config.GOOGLE_TOKEN.write_text(creds.to_json())
            _cache["mtime"] = config.GOOGLE_TOKEN.stat().st_mtime
        return creds if creds.valid else None


def login():
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not config.GOOGLE_CREDENTIALS.exists():
        raise SystemExit("Missing credentials.json. See README -> Google setup.")
    # Google lets the user untick individual permissions; accept a partial grant instead of crashing.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = InstalledAppFlow.from_client_secrets_file(str(config.GOOGLE_CREDENTIALS), config.GOOGLE_SCOPES)
    # prompt=consent makes Google return a refresh token every time, so the login survives restarts.
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    granted = getattr(creds, "granted_scopes", None)
    if granted:  # record what Google actually granted, not what was asked for
        info = json.loads(creds.to_json())
        info["scopes"] = sorted(granted)
        config.GOOGLE_TOKEN.write_text(json.dumps(info))
    else:
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
