"""Google login. Run once:  python -m app.google_auth

Needs credentials.json (OAuth client, type "Desktop app") in the repo root.
Add each teammate's Gmail as a Test user on the OAuth consent screen.
"""
from . import config


def load_credentials():
    """Return valid Google credentials, or None if nobody has logged in yet."""
    if not config.GOOGLE_TOKEN.exists():
        return None
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(config.GOOGLE_TOKEN), config.GOOGLE_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        config.GOOGLE_TOKEN.write_text(creds.to_json())
    return creds if creds.valid else None


def login():
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not config.GOOGLE_CREDENTIALS.exists():
        raise SystemExit("Missing credentials.json. See README -> Google setup.")
    flow = InstalledAppFlow.from_client_secrets_file(str(config.GOOGLE_CREDENTIALS), config.GOOGLE_SCOPES)
    creds = flow.run_local_server(port=0)
    config.GOOGLE_TOKEN.write_text(creds.to_json())
    print("Logged in. token.json written.")


if __name__ == "__main__":
    login()
