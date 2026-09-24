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


def test_equity_buy_rounds_fractional_shares_instead_of_truncating():
    """API fills like 9.9 must become 10, matching whole-share statements."""
    txn = {
        "activityId": "lulu-frac",
        "tradeDate": "2025-07-24T05:00:00+0000",
        "type": "TRADE",
        "orderId": 99001,
        "description": "Buy LULU",
        "netAmount": -2189.90,
        "transferItems": [
            {
                "instrument": {"assetType": "EQUITY", "symbol": "LULU"},
                "amount": 9.9,
                "price": 218.99,
                "cost": -2168.00,
                "instruction": "BUY",
                "positionEffect": "OPENING",
            }
        ],
    }
    parsed = classify_transactions([txn])
    assert len(parsed.equity_buys) == 1
    assert parsed.equity_buys[0].shares == 10
    assert parsed.equity_buys[0].ticker == "LULU"


def test_equity_sell_rounds_fractional_shares_instead_of_truncating():
    txn = {
        "activityId": "lulu-sell-frac",
        "tradeDate": "2026-06-01T05:00:00+0000",
        "type": "TRADE",
        "orderId": 99002,
        "description": "Sell LULU",
        "netAmount": 25199.44,
        "transferItems": [
            {
                "instrument": {"assetType": "EQUITY", "symbol": "LULU"},
                "amount": -199.6,
                "price": 126,
                "cost": 25199.44,
                "instruction": "SELL",
                "positionEffect": "CLOSING",
            }
        ],
    }
    parsed = classify_transactions([txn])
    assert len(parsed.equity_sells) == 1
    assert parsed.equity_sells[0].shares == 200
    parsed = classify_transactions(_rows())
    assert len(parsed.equity_sells) == 1
    sell = parsed.equity_sells[0]
    assert sell.activity_id == "2002"
    assert sell.ticker == "AAPL"
    assert sell.shares == 5
    assert sell.price == Decimal("160")
    assert sell.fees == Decimal("0")
    assert sell.order_id == "88002"


def test_review_covers_interest_drip_option_assignment_journal():
    parsed = classify_transactions(_rows())
    reasons = {item.reason: item.activity_id for item in parsed.review}
    assert reasons["interest"] == "1002"
    assert reasons["drip"] == "1003"
    assert reasons["option_leg"] in {"2003", "2004"}
    assert "unsupported_type:JOURNAL" in reasons
    assert "equity_sell" not in reasons
    assert {item.activity_id for item in parsed.review} >= {
        "1002",
        "1003",
        "2003",
        "2004",
        "3001",
    }
