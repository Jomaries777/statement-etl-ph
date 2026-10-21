"""Google OAuth for Gmail: one consent in the browser, one token on disk.

Each user brings their own OAuth client (a "Desktop app" client from their own
Google Cloud project). This package never ships one: Gmail read access is a
restricted scope, and a shared public client would need Google's verification
and a security assessment.

The token is stored locally, readable only by you (mode ``0600``), and is never
logged. Environment variables override it, which is how a scheduled run can
supply it without a file:

    GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN

A refresh token from an OAuth app left in "Testing" expires after seven days.
Publish the app (Testing -> In production) to keep it; see the README.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

TOKEN_URI = "https://oauth2.googleapis.com/token"

# gmail.modify is the narrowest scope that can apply a label, and the label is
# what stops the next run fetching the same message again. gmail.readonly is
# offered for people who do not want this tool changing their mailbox; the
# sink's duplicate key is then the only guard against importing twice.
SCOPE_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"

_ENV = (
    ("GMAIL_CLIENT_ID", "client_id"),
    ("GMAIL_CLIENT_SECRET", "client_secret"),
    ("GMAIL_REFRESH_TOKEN", "refresh_token"),
)


def config_dir() -> Path:
    """Where the token and the config file live by default."""
    if os.name == "nt" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"])
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "statement-etl-ph"


def default_token_path() -> Path:
    return config_dir() / "token.json"


def write_private(path: Path, text: str) -> None:
    """Write a file only its owner can read, creating its folder if needed.

    The mode is set when the file is created, not afterwards, so there is no
    moment when the secret sits on disk readable by anyone else.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    path.chmod(0o600)  # O_CREAT ignores the mode when the file already existed


def save_token(path: Path, token: dict) -> None:
    write_private(path, json.dumps(token, indent=2) + "\n")


def load_token(path: Optional[Path] = None) -> dict:
    """The stored token, with any GMAIL_* environment variables applied over it.

    Raises ``FileNotFoundError`` naming the command to run when there is neither.
    """
    path = Path(path) if path else default_token_path()
    token: dict = {}
    if path.exists():
        token = json.loads(path.read_text(encoding="utf-8"))
    for env, key in _ENV:
        if os.environ.get(env):
            token[key] = os.environ[env]
    missing = [key for _, key in _ENV if not token.get(key)]
    if missing:
        raise FileNotFoundError(
            f"no Gmail token at {path} (missing {', '.join(missing)}). "
            "Run: statement-etl-ph auth path/to/client_secret.json"
        )
    token.setdefault("scopes", [SCOPE_MODIFY])
    return token


def run_consent_flow(client_secrets: Path, *, readonly: bool = False) -> dict:
    """Open the browser for consent and return the token to save.

    ``access_type=offline`` with ``prompt=consent`` is what makes Google hand
    back a refresh token rather than only a short-lived access token.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    scopes = [SCOPE_READONLY if readonly else SCOPE_MODIFY]
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), scopes)
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    installed = json.loads(Path(client_secrets).read_text(encoding="utf-8"))
    client = installed.get("installed") or installed.get("web") or {}
    return {
        "client_id": client.get("client_id", ""),
        "client_secret": client.get("client_secret", ""),
        "refresh_token": credentials.refresh_token,
        "scopes": scopes,
    }


def build_credentials(token: dict):
    """Google user credentials from a stored token."""
    from google.oauth2.credentials import Credentials

    return Credentials(
        token=None,
        refresh_token=token["refresh_token"],
        client_id=token["client_id"],
        client_secret=token["client_secret"],
        token_uri=TOKEN_URI,
        scopes=list(token.get("scopes") or [SCOPE_MODIFY]),
    )


def can_label(token: dict) -> bool:
    """Was this token granted the scope that can mark messages processed?"""
    return SCOPE_MODIFY in (token.get("scopes") or [SCOPE_MODIFY])
