"""Thin Schwab Trader API HTTP wrapper.

Fetches account hashes and transactions. Auth refresh lives in
`schwab_tokens.py`. Does not classify or write to the database.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from scripts.schwab_tokens import DEFAULT_TOKEN_PATH, ensure_access_token

TRADER_BASE = "https://api.schwabapi.com/trader/v1"
# v1 of the importer only asks Schwab for trades and dividend/interest rows.
TRANSACTION_TYPES = "TRADE,DIVIDEND_OR_INTEREST"


class SchwabApiError(RuntimeError):
    """Trader API request failed."""


def _iso_z(day: date, *, end_of_day: bool = False) -> str:
    hour = 23 if end_of_day else 0
    minute = 59 if end_of_day else 0
    second = 59 if end_of_day else 0
    dt = datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class SchwabClient:
    def __init__(
        self,
        *,
        tokens_path: Path = DEFAULT_TOKEN_PATH,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._tokens_path = tokens_path
        self._client = httpx.Client(base_url=TRADER_BASE, timeout=60.0, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SchwabClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        token = ensure_access_token(self._tokens_path)
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _get(self, path: str, params: dict[str, str] | None = None) -> httpx.Response:
        response = self._client.get(path, params=params, headers=self._headers())
        if response.status_code == 401:
            # Force a refresh by pretending the token is expired, then retry once.
            ensure_access_token(self._tokens_path)
            response = self._client.get(path, params=params, headers=self._headers())
        if response.status_code >= 400:
            raise SchwabApiError(f"GET {path} returned HTTP {response.status_code}")
        return response

    def list_account_hashes(self) -> list[dict[str, str]]:
        payload = self._get("/accounts/accountNumbers").json()
        if not isinstance(payload, list):
            raise SchwabApiError("Unexpected accountNumbers payload")
        result: list[dict[str, str]] = []
        for row in payload:
            hash_value = str(row.get("hashValue") or "")
            account_number = str(row.get("accountNumber") or "")
            if hash_value:
                result.append({"hashValue": hash_value, "accountNumber": account_number})
        return result

    def fetch_transactions(
        self,
        account_hash: str,
        *,
        start: date,
        end: date,
        types: str = TRANSACTION_TYPES,
    ) -> list[dict[str, Any]]:
        params = {
            "startDate": _iso_z(start, end_of_day=False),
            "endDate": _iso_z(end, end_of_day=True),
            "types": types,
        }
        payload = self._get(
            f"/accounts/{account_hash}/transactions",
            params=params,
        ).json()
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise SchwabApiError("Unexpected transactions payload")
        return payload
