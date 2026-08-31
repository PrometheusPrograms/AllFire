"""Schwab OAuth token file helpers — never log token values.

Tokens live in `backend/.schwab_tokens.json` (gitignored). Access tokens
are short-lived; refresh happens here so the trader client stays dumb.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import settings

AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
DEFAULT_TOKEN_PATH = Path(__file__).resolve().parent.parent / ".schwab_tokens.json"
ACCESS_TOKEN_SKEW_SECONDS = 60


class SchwabAuthError(RuntimeError):
    """OAuth/token exchange failed."""


def authorization_url(client_id: str, redirect_uri: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def exchange_code(
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict[str, Any]:
    return _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        client_id=client_id,
        client_secret=client_secret,
    )


def refresh_access_token(
    *,
    refresh_token: str,
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    return _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        client_id=client_id,
        client_secret=client_secret,
    )


def _token_request(
    data: dict[str, str],
    *,
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    headers = {
        "Authorization": _basic_auth_header(client_id, client_secret),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    response = httpx.post(TOKEN_URL, data=data, headers=headers, timeout=30.0)
    if response.status_code >= 400:
        raise SchwabAuthError(f"Schwab token endpoint returned HTTP {response.status_code}")
    payload = response.json()
    expires_in = int(payload.get("expires_in") or 1800)
    payload["expires_at"] = time.time() + expires_in - ACCESS_TOKEN_SKEW_SECONDS
    return payload


def load_tokens(path: Path = DEFAULT_TOKEN_PATH) -> dict[str, Any]:
    if not path.exists():
        raise SchwabAuthError(
            f"No token file at {path}. Run `python -m scripts.schwab_auth` first."
        )
    return json.loads(path.read_text())


def save_tokens(payload: dict[str, Any], path: Path = DEFAULT_TOKEN_PATH) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n")
    path.chmod(0o600)


def ensure_access_token(
    path: Path = DEFAULT_TOKEN_PATH,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> str:
    """Return a (possibly refreshed) access token. Writes the file if refreshed."""
    tokens = load_tokens(path)
    access = tokens.get("access_token")
    if not access:
        raise SchwabAuthError("Token file is missing access_token.")
    expires_at = float(tokens.get("expires_at") or 0)
    if time.time() < expires_at:
        return str(access)

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise SchwabAuthError("Access token expired and no refresh_token is stored.")

    cid = client_id if client_id is not None else settings.schwab_client_id
    secret = client_secret if client_secret is not None else settings.schwab_client_secret
    if not cid or not secret:
        raise SchwabAuthError(
            "SCHWAB_CLIENT_ID and SCHWAB_CLIENT_SECRET must be set to refresh tokens."
        )
    refreshed = refresh_access_token(
        refresh_token=str(refresh_token),
        client_id=cid,
        client_secret=secret,
    )
    # Schwab sometimes omits a new refresh token; keep the old one.
    if not refreshed.get("refresh_token"):
        refreshed["refresh_token"] = refresh_token
    save_tokens(refreshed, path)
    return str(refreshed["access_token"])
