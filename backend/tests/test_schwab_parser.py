"""Tests for scripts/schwab_parser.py against anonymized Schwab JSON."""

import json
from decimal import Decimal
from pathlib import Path

from scripts.schwab_parser import classify_transactions

FIXTURE = Path(__file__).parent / "fixtures" / "schwab_transactions.json"


def _rows() -> list[dict]:
    return json.loads(FIXTURE.read_text())


def test_classifies_cash_dividend_and_equity_buy():
    parsed = classify_transactions(_rows())
    assert len(parsed.dividends) == 1
    dividend = parsed.dividends[0]
    assert dividend.activity_id == "1001"
    assert dividend.ticker == "AAPL"
    assert dividend.amount == Decimal("12.50")

    assert len(parsed.equity_buys) == 1
    buy = parsed.equity_buys[0]
    assert buy.activity_id == "2001"
    assert buy.ticker == "AAPL"
    assert buy.shares == 10
    assert buy.price == Decimal("150")
    assert buy.fees == Decimal("0.65")
    assert buy.order_id == "88001"


def test_review_covers_interest_drip_sell_option_assignment_journal():
    parsed = classify_transactions(_rows())
    reasons = {item.reason: item.activity_id for item in parsed.review}
    assert reasons["interest"] == "1002"
    assert reasons["drip"] == "1003"
    assert reasons["equity_sell"] == "2002"
    assert reasons["option_leg"] in {"2003", "2004"}
    assert "unsupported_type:JOURNAL" in reasons
    assert {item.activity_id for item in parsed.review} >= {
        "1002",
        "1003",
        "2002",
        "2003",
        "2004",
        "3001",
    }
