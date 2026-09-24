"""Tests for scripts/okw_parser.py against a small synthetic workbook that
mirrors the real OKW layout: column B holds row labels, and each trade's
values live in whichever column its row-1 header cell occupies (columns are
not assumed to be a fixed 2 apart — see test for an irregular spread case).
"""

from datetime import date
from decimal import Decimal

import pytest
from openpyxl import Workbook

from scripts.okw_parser import parse_trade_sheet

# (label, row) pairs mirroring the real sheet's row numbers closely enough
# to exercise the parser, but condensed — the parser doesn't care about
# exact row numbers, only about scanning column B to find them.
ROWS = {
    "TRADE DATE": 3,
    "UNDERLYING": 4,
    "PRICE": 13,
    "DAYS TO EXPIRATION (DTE)": 15,
    "EXPIRATION DATE": 16,
    "PROBABILITY OF WINNING": 21,
    "DELTA": 22,
    "SHORT STRIKE": 23,
    "LONG STRIKE": 26,
    "CREDIT/(DEBIT)": 29,
    "COMMISSION (Per share)": 30,
    "NET CREDIT (NC)": 31,
    "RISK CAPITAL (RC)": 32,
    "MARGIN %": 33,
    "MARGIN CAPITAL": 34,
    "ANNUALIZED RORC (ARORC)": 37,
    "ACTUAL CONTRACTS": 49,
    "SHARES": 50,
    "RESULT": 65,
    " RESULT DATE (Closed or Expired)": 66,
    "CLOSING DEBIT": 67,
    "TOTAL DEBIT": 68,
    "NOTES": 72,
}


def _make_workbook():
    wb = Workbook()
    ws = wb.active
    ws.title = "TRADES 2026"
    for label, row in ROWS.items():
        ws.cell(row=row, column=2, value=label)
    return ws


def _set_block(ws, col: int, header: str, values: dict):
    ws.cell(row=1, column=col, value=header)
    for label, value in values.items():
        ws.cell(row=ROWS[label], column=col, value=value)


def test_parses_a_single_leg_put_block():
    ws = _make_workbook()
    _set_block(
        ws,
        5,  # column E
        "SLV ROCT PUT",
        {
            "TRADE DATE": "2026-01-02",
            "UNDERLYING": "SLV",
            "PRICE": 85.04,
            "DAYS TO EXPIRATION (DTE)": 7,
            "EXPIRATION DATE": "2026-01-09",
            "PROBABILITY OF WINNING": 0.85,
            "DELTA": 0.15,
            "SHORT STRIKE": 55.5,
            "CREDIT/(DEBIT)": 0.22,
            "COMMISSION (Per share)": 0.0041,
            "NET CREDIT (NC)": 0.2159,
            "RISK CAPITAL (RC)": 55.2841,
            "MARGIN %": 1,
            "MARGIN CAPITAL": 11056.82,
            "ANNUALIZED RORC (ARORC)": 0.2036,
            "ACTUAL CONTRACTS": 2,
            "SHARES": 200,
            "RESULT": "EXPIRED",
            " RESULT DATE (Closed or Expired)": "2026-01-22",
            "TOTAL DEBIT": 0,
        },
    )

    [trade] = parse_trade_sheet(ws)

    assert trade.ticker == "SLV"
    assert trade.trade_type_name == "ROCT PUT"
    assert trade.price_per_share == Decimal("85.04")
    assert trade.short_strike == Decimal("55.5")
    assert trade.long_strike is None  # not a spread — never populated
    assert trade.num_of_contracts == 2
    assert trade.num_of_shares == 200
    assert trade.event_type == "EXPIRE"
    assert trade.delta == Decimal("0.15")
    assert trade.probability_of_winning == Decimal("0.85")
    assert trade.needs_review is False


def test_parses_a_spread_block_and_keeps_long_strike():
    ws = _make_workbook()
    _set_block(
        ws,
        5,
        "SFM ROCS BULL PUT SPREAD",
        {
            "TRADE DATE": "2026-02-01",
            "UNDERLYING": "SFM",
            "PRICE": 60.0,
            "SHORT STRIKE": 55.5,
            "LONG STRIKE": 50.0,
            "RESULT": None,
        },
    )

    [trade] = parse_trade_sheet(ws)

    assert trade.trade_type_name == "ROCS BULL PUT SPREAD"
    assert trade.long_strike == Decimal("50.0")
    assert trade.event_type is None  # still open


def test_handles_covered_call_margin_capital_text_placeholder():
    ws = _make_workbook()
    _set_block(
        ws,
        5,
        "CROX ROCT CALL",
        {
            "UNDERLYING": "CROX",
            "MARGIN CAPITAL": "COVERED",
            "RESULT": None,
        },
    )

    [trade] = parse_trade_sheet(ws)

    assert trade.margin_capital is None  # non-numeric placeholder, not an error


def test_unrecognized_result_value_is_flagged_for_review_not_guessed():
    ws = _make_workbook()
    _set_block(
        ws,
        5,
        "NKE ROCT PUT",
        {"UNDERLYING": "NKE", "RESULT": "SOMETHING WEIRD"},
    )

    [trade] = parse_trade_sheet(ws)

    assert trade.event_type is None
    assert trade.needs_review is True
    assert any("SOMETHING WEIRD" in w for w in trade.warnings)


def test_discovers_blocks_at_irregular_column_spacing():
    """Real sheet has adjacent (not 2-apart) blocks in places — column
    discovery must not assume a fixed stride.
    """
    ws = _make_workbook()
    _set_block(ws, 5, "SHOP ROCS BULL PUT SPREAD", {"UNDERLYING": "SHOP"})  # E
    _set_block(ws, 6, "SHOP RULE ONE PUT", {"UNDERLYING": "SHOP"})  # F — 1 apart

    trades = parse_trade_sheet(ws)

    assert len(trades) == 2
    assert trades[0].column_letter == "E"
    assert trades[1].column_letter == "F"


def test_missing_expected_labels_raises_instead_of_silently_parsing_garbage():
    wb = Workbook()
    ws = wb.active
    ws.title = "NOT AN OKW SHEET"
    ws.cell(row=1, column=1, value="hello")

    with pytest.raises(ValueError, match="doesn't look like an OKW trade sheet"):
        parse_trade_sheet(ws)


def test_formula_header_uses_underlying_and_quoted_type():
    ws = _make_workbook()
    _set_block(
        ws,
        5,
        '=E4&" ROCT PUT"',
        {"UNDERLYING": "RBLX", "PRICE": 50.16, "RESULT": None},
    )

    [trade] = parse_trade_sheet(ws)

    assert trade.ticker == "RBLX"
    assert trade.trade_type_name == "ROCT PUT"


def test_prob_otm_derived_from_delta_when_pow_is_dash():
    ws = _make_workbook()
    _set_block(
        ws,
        5,
        "RBLX ROCT PUT",
        {
            "UNDERLYING": "RBLX",
            "DELTA": 0.05,
            "PROBABILITY OF WINNING": "-",
            "RESULT": None,
        },
    )

    [trade] = parse_trade_sheet(ws)

    assert trade.delta == Decimal("0.05")
    assert trade.probability_of_winning == Decimal("0.950000")


def test_parses_rblx_roll_columns_like_okw_sheet():
    """Golden two-column roll matching the live OKW RBLX ROCT PUT example."""
    ws = _make_workbook()
    ws.cell(row=57, column=2, value="DELTA")
    ws.cell(row=58, column=2, value="PROBABILITY OF WINNING")
    ws.cell(row=69, column=2, value="NET CREDIT/(DEBIT)")
    ws.cell(row=71, column=2, value="FINAL ARORC")

    _set_block(
        ws,
        5,
        "RBLX ROCT PUT",
        {
            "TRADE DATE": date(2026, 7, 22),
            "UNDERLYING": "RBLX",
            "PRICE": 50.16,
            "DAYS TO EXPIRATION (DTE)": 9,
            "EXPIRATION DATE": date(2026, 7, 31),
            "DELTA": 0.05,
            "PROBABILITY OF WINNING": 0.95,
            "SHORT STRIKE": 36.00,
            "CREDIT/(DEBIT)": 0.21,
            "COMMISSION (Per share)": 0.00410,
            "NET CREDIT (NC)": 0.2059,
            "ANNUALIZED RORC (ARORC)": 0.233,
            "ACTUAL CONTRACTS": 1,
            "SHARES": 100,
            "RESULT": "ROLL",
            " RESULT DATE (Closed or Expired)": date(2026, 7, 31),
            "CLOSING DEBIT": 0,
            "TOTAL DEBIT": 0,
        },
    )
    ws.cell(row=69, column=5, value=20.59)
    ws.cell(row=71, column=5, value=0.233)

    _set_block(
        ws,
        6,
        '=F4&" ROCT PUT"',
        {
            "TRADE DATE": date(2026, 7, 31),
            "UNDERLYING": "RBLX",
            "PRICE": 50.16,
            "DAYS TO EXPIRATION (DTE)": 7,
            "EXPIRATION DATE": date(2026, 8, 7),
            "DELTA": 0.05,
            "PROBABILITY OF WINNING": 0.95,
            "SHORT STRIKE": 34.50,
            "CREDIT/(DEBIT)": 0.02,
            "COMMISSION (Per share)": 0.00410,
            "NET CREDIT (NC)": 0.0159,
            "ANNUALIZED RORC (ARORC)": 0.024,
            "ACTUAL CONTRACTS": 1,
            "SHARES": 100,
            "RESULT": "EXPIRED",
            "CLOSING DEBIT": 0,
            "TOTAL DEBIT": 0,
        },
    )
    ws.cell(row=69, column=6, value=22.18)
    ws.cell(row=71, column=6, value=0.335)

    first, second = parse_trade_sheet(ws)

    assert first.ticker == "RBLX"
    assert first.trade_date == date(2026, 7, 22)
    assert first.price_per_share == Decimal("50.16")
    assert first.days_to_expiration == 9
    assert first.expiration_date == date(2026, 7, 31)
    assert first.delta == Decimal("0.05")
    assert first.probability_of_winning == Decimal("0.95")
    assert first.short_strike == Decimal("36")
    assert first.credit_debit == Decimal("0.21")
    assert first.event_type == "ROLL"
    assert first.result_date == date(2026, 7, 31)
    assert first.result_net_credit == Decimal("20.59")
    assert first.final_arorc == Decimal("0.233")
    assert first.arorc == Decimal("0.233")

    assert second.ticker == "RBLX"
    assert second.trade_date == date(2026, 7, 31)
    assert second.days_to_expiration == 7
    assert second.expiration_date == date(2026, 8, 7)
    assert second.delta == Decimal("0.05")
    assert second.probability_of_winning == Decimal("0.95")
    assert second.short_strike == Decimal("34.5")
    assert second.credit_debit == Decimal("0.02")
    assert second.event_type == "EXPIRE"
    assert second.result_date is None
    assert second.result_net_credit == Decimal("22.18")
    assert second.final_arorc == Decimal("0.335")
    assert second.arorc == Decimal("0.024")
