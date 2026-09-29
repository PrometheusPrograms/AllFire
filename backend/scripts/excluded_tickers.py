"""Tickers the user keeps out of the app entirely (`data/excluded_tickers.json`).

Checked by every importer that can create trades, lots or dividends, so the
exclusion holds on dev, staging and production alike.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DEFAULT_FILE = Path(__file__).with_name("data") / "excluded_tickers.json"


@lru_cache(maxsize=None)
def _load(path: Path = DEFAULT_FILE) -> tuple[frozenset[str], frozenset[str]]:
    data = json.loads(path.read_text())
    return (
        frozenset(t.strip().upper() for t in data["tickers"]),
        frozenset(d.strip().upper() for d in data.get("description_prefixes", [])),
    )


def is_excluded(ticker: str | None = None, description: str | None = None) -> bool:
    tickers, prefixes = _load()
    name = (description or "").strip().upper()
    return bool(
        (ticker and ticker.strip().upper() in tickers)
        or (name and any(name.startswith(prefix) for prefix in prefixes))
    )
