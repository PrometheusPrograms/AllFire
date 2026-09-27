"""Tests for the statement-dividend backfill planner (pure, no PDFs/DB)."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from scripts.backfill_statement_dividends import (
    DbDividend,
    StatementDividend,
    dedupe_statement_dividends,
    dividends_from_current_rows,
    dividends_from_legacy_rows,
    plan_dividend_backfill,
)


def _stmt(symbol, paid_on, amount, account="Rule 1", source="742"):
    return StatementDividend(
        source, account, symbol, paid_on, Decimal(amount), f"{symbol} dividend"
    )


def _db(id_, paid_on, amount, *, ticker=None, description="OCCIDENTAL PETE CORP", account="Rule 1"):
    return DbDividend(id_, account, ticker, paid_on, Decimal(amount), description)


def test_links_unlinked_row_on_account_date_amount():
    plan = plan_dividend_backfill(
        [_stmt("OXY", date(2025, 4, 15), "273.30")], [_db(1, date(2025, 4, 15), "273.30")]
    )
    assert plan.link == [(1, "OXY")]
    assert plan.insert == []


def test_inserts_statement_dividend_with_no_db_row():
    div = _stmt("GDXY", date(2023, 12, 1), "12.45")
    plan = plan_dividend_backfill([div], [])
    assert plan.insert == [div]


def test_relabels_bank_interest_and_never_links_it():
    interest = _db(
        7, date(2024, 1, 16), "10.09", description="BANK INT 121623-011524 TD BANK USA NA"
    )
    plan = plan_dividend_backfill([], [interest])
    assert plan.relabel_interest == [7]
    assert plan.link == []


def test_already_linked_row_is_left_alone_and_not_reinserted():
    plan = plan_dividend_backfill(
        [_stmt("OXY", date(2025, 4, 15), "273.30")],
        [_db(1, date(2025, 4, 15), "273.30", ticker="OXY")],
    )
    assert plan.link == [] and plan.insert == []


def test_same_day_same_amount_rows_are_told_apart_by_description():
    # Two tickers pay $80.50 on the same day in one account; one description is
    # already known from an unambiguous match elsewhere.
    divs = [
        _stmt("GDX", date(2025, 3, 3), "11.11"),
        _stmt("GDX", date(2025, 12, 22), "80.50"),
        _stmt("OXY", date(2025, 12, 22), "80.50"),
    ]
    rows = [
        _db(1, date(2025, 3, 3), "11.11", description="VANECK GOLD MINERS ETF"),
        _db(2, date(2025, 12, 22), "80.50", description="OCCIDENTAL PETE CORP"),
        _db(3, date(2025, 12, 22), "80.50", description="VANECK GOLD MINERS ETF"),
    ]
    plan = plan_dividend_backfill(divs, rows)
    assert sorted(plan.link) == [(1, "GDX"), (2, "OXY"), (3, "GDX")]
    assert plan.insert == []


def test_identical_statement_descriptions_resolved_from_later_payment():
    # Statements print GOOG and GOOGL both as "ALPHABETINC"; the DB rows carry
    # the class. A later GOOGL-only payment teaches that "CLASS A" is GOOGL —
    # even though Schwab spelled it differently there ("CLASS CLASS A").
    def alpha(sym, d, amt):
        return StatementDividend("742", "Rule 1", sym, d, Decimal(amt), "ALPHABETINC")

    divs = [
        alpha("GOOG", date(2025, 9, 15), "21.00"),
        alpha("GOOGL", date(2025, 9, 15), "21.00"),
        alpha("GOOGL", date(2026, 3, 16), "21.00"),
    ]
    rows = [
        _db(1, date(2025, 9, 15), "21.00", description="ALPHABET INC CLASS A"),
        _db(2, date(2025, 9, 15), "21.00", description="ALPHABET INC CLASS C"),
        _db(3, date(2026, 3, 16), "21.00", description="ALPHABET INC CLASS CLASS A"),
    ]
    plan = plan_dividend_backfill(divs, rows)
    assert sorted(plan.link) == [(1, "GOOGL"), (2, "GOOG"), (3, "GOOGL")]
    assert plan.insert == []


def test_margin_interest_adjustment_is_relabelled():
    plan = plan_dividend_backfill(
        [], [_db(96, date(2026, 5, 26), "100.00", description="MGN INT ADJ")]
    )
    assert plan.relabel_interest == [96]


def test_matches_within_date_tolerance_when_no_exact_date():
    plan = plan_dividend_backfill(
        [_stmt("OXY", date(2025, 4, 15), "273.30")], [_db(1, date(2025, 4, 17), "273.30")]
    )
    assert plan.link == [(1, "OXY")] and plan.insert == []


def test_other_account_row_is_not_matched():
    plan = plan_dividend_backfill(
        [_stmt("OXY", date(2025, 4, 15), "273.30")],
        [_db(1, date(2025, 4, 15), "273.30", account="Roth")],
    )
    assert plan.link == [] and len(plan.insert) == 1


def test_dedupe_keeps_same_payment_in_two_source_accounts():
    a = _stmt("AWK", date(2021, 12, 1), "41.01", source="641")
    b = _stmt("AWK", date(2021, 12, 1), "41.01", source="742")
    assert dedupe_statement_dividends([a, a, b]) == [a, b]


def test_reinvested_dividend_is_removed_from_db_and_never_inserted():
    reinvested = StatementDividend(
        "467", "Roth", "CWT", date(2024, 11, 22), Decimal("9.87"), "CWT", True
    )
    missing_reinvested = StatementDividend(
        "467", "Roth", "CWT", date(2023, 11, 22), Decimal("9.50"), "CWT", True
    )
    plan = plan_dividend_backfill(
        [reinvested, missing_reinvested],
        [
            _db(
                5,
                date(2024, 11, 22),
                "9.87",
                description="CALIFORNIA WTR SVC GROUP",
                account="Roth",
            )
        ],
    )
    assert plan.remove_reinvested == [5]
    assert plan.link == [] and plan.insert == []


def test_legacy_dividend_with_same_day_buy_is_reinvested():
    legacy = [
        SimpleNamespace(
            activity="Div/Int - Income",
            symbol="SERVICE CWT",
            trade_date=date(2021, 11, 19),
            settle_date=None,
            amount=Decimal("7.67"),
            description="CALIFORNIA WATER GROUP",
        ),
        SimpleNamespace(
            activity="Buy - Securities Purchased",
            symbol="SERVICE CWT",
            trade_date=date(2021, 11, 19),
            settle_date=None,
            amount=Decimal("-7.67"),
            description="CALIFORNIA WATER GROUP",
        ),
        SimpleNamespace(
            activity="Div/Int - Income",
            symbol="VXUS",
            trade_date=date(2021, 12, 23),
            settle_date=None,
            amount=Decimal("17.46"),
            description="VANGUARD TL INTL",
        ),
    ]
    flags = {d.symbol: d.reinvested for d in dividends_from_legacy_rows("467", legacy)}
    assert flags == {"CWT": True, "VXUS": False}


def test_current_reinvest_actions_are_flagged():
    rows = [
        SimpleNamespace(
            category="Dividend",
            action="QualDivReinvest",
            symbol_cusip="OXY",
            date=date(2025, 1, 15),
            amount=Decimal("198.00"),
            description="OXY",
        ),
        SimpleNamespace(
            category="Dividend",
            action="Qual.Dividend",
            symbol_cusip="OXY",
            date=date(2025, 4, 15),
            amount=Decimal("273.30"),
            description="OXY",
        ),
    ]
    assert [(d.paid_on.month, d.reinvested) for d in dividends_from_current_rows("742", rows)] == [
        (1, True),
        (4, False),
    ]


def test_parses_current_and_legacy_rows():
    current = [
        SimpleNamespace(
            category="Dividend",
            action="CashDividend",
            symbol_cusip="GDXY",
            date=date(2026, 8, 28),
            amount=Decimal("23.63"),
            description="YIELDMAX",
        ),
        SimpleNamespace(
            category="Interest",
            action="CreditInterest",
            symbol_cusip=None,
            date=date(2026, 8, 28),
            amount=Decimal("1.00"),
            description="INT",
        ),
    ]
    legacy = [
        SimpleNamespace(
            activity="Div/Int - Income",
            symbol="CO INC AWK",
            trade_date=date(2021, 12, 1),
            settle_date=None,
            amount=Decimal("41.01"),
            description="AMER WATER WORKS",
        ),
        SimpleNamespace(
            activity="Div/Int - Other",
            symbol="MMDA1",
            trade_date=date(2021, 12, 1),
            settle_date=None,
            amount=Decimal("0.04"),
            description="FDIC INSURED DEPOSIT",
        ),
    ]
    assert [(d.symbol, d.account_name) for d in dividends_from_current_rows("467", current)] == [
        ("GDXY", "Roth")
    ]
    assert [(d.symbol, d.account_name) for d in dividends_from_legacy_rows("641", legacy)] == [
        ("AWK", "Rule 1")
    ]
