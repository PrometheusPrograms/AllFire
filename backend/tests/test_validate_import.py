"""Tests for scripts.validate_import comparison logic."""

import json
import sqlite3
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models import Account, Bankroll, Base, CostBasis, Ticker, Trade, TradeType
from scripts.validate_import import (
    BankrollYearStart,
    ExpectedTotals,
    PremiumTotal,
    check_trade_math,
    load_expected,
    reconcile_bankroll,
    reconcile_premium,
    run_validation,
    walk_cost_basis,
)


def _option_trade(**overrides):
    strike = Decimal("217.5")
    premium = Decimal("0.83")
    commission = Decimal("0.0041")
    net = premium - commission
    values = dict(
        id=1,
        credit_debit=premium,
        commission_per_share=commission,
        net_credit_per_share=net,
        strike_price=strike,
        long_strike=None,
        risk_capital_per_share=strike - net,
        margin_percent=Decimal("1"),
        days_to_expiration=7,
        arorc=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_check_trade_math_clean_when_stored_matches_recompute():
    trade = _option_trade()
    assert check_trade_math(trade, "ROCT PUT", "OPTIONS") == []


def test_check_trade_math_ignores_numeric_6_rounding():
    trade = _option_trade(arorc=Decimal("0.198754"))
    assert check_trade_math(trade, "ROCT PUT", "OPTIONS") == []
    trade = _option_trade(arorc=Decimal("0.99"))
    issues = check_trade_math(trade, "ROCT PUT", "OPTIONS")
    assert any(i.check == "trade_math" and "arorc" in i.detail for i in issues)


def test_check_trade_math_flags_wrong_net_credit():
    trade = _option_trade(net_credit_per_share=Decimal("9.99"))
    issues = check_trade_math(trade, "ROCT PUT", "OPTIONS")
    assert any("net_credit_per_share" in i.detail for i in issues)


def test_check_trade_math_skips_stock():
    trade = _option_trade()
    assert check_trade_math(trade, "BTO", "STOCK") == []


def test_walk_cost_basis_flags_negative_running_shares():
    rows = [
        CostBasis(
            id=1,
            account_id=1,
            ticker_id=1,
            trade_id=10,
            transaction_date=date(2025, 1, 2),
            shares=10,
            cost_per_share=Decimal("10"),
            total_amount=Decimal("100"),
        ),
        CostBasis(
            id=2,
            account_id=1,
            ticker_id=1,
            trade_id=11,
            transaction_date=date(2025, 1, 3),
            shares=-20,
            cost_per_share=Decimal("10"),
            total_amount=Decimal("-200"),
        ),
    ]
    issues = walk_cost_basis(rows)
    assert any("negative running_shares" in i.detail for i in issues)


def test_walk_cost_basis_flags_implausible_swing():
    rows = [
        CostBasis(
            id=1,
            account_id=1,
            ticker_id=1,
            trade_id=10,
            transaction_date=date(2025, 1, 2),
            shares=10,
            cost_per_share=Decimal("10"),
            total_amount=Decimal("100"),
        ),
        CostBasis(
            id=2,
            account_id=1,
            ticker_id=1,
            trade_id=11,
            transaction_date=date(2025, 1, 3),
            shares=10,
            cost_per_share=Decimal("1000"),
            total_amount=Decimal("10000"),
        ),
    ]
    issues = walk_cost_basis(rows)
    assert any("implausible basis_per_share" in i.detail for i in issues)


def test_walk_cost_basis_ignores_split_and_sale_swings():
    split_rows = [
        CostBasis(
            id=1,
            account_id=1,
            ticker_id=1,
            trade_id=10,
            transaction_date=date(2022, 1, 2),
            shares=5,
            cost_per_share=Decimal("600"),
            total_amount=Decimal("3000"),
        ),
        CostBasis(
            id=2,
            account_id=1,
            ticker_id=1,
            trade_id=11,
            transaction_date=date(2022, 7, 18),
            shares=95,
            cost_per_share=Decimal("0"),
            total_amount=Decimal("0"),
        ),
    ]
    assert walk_cost_basis(split_rows) == []

    sale_rows = [
        CostBasis(
            id=1,
            account_id=1,
            ticker_id=2,
            trade_id=20,
            transaction_date=date(2015, 8, 28),
            shares=14,
            cost_per_share=Decimal("135.53"),
            total_amount=Decimal("1897.42"),
        ),
        CostBasis(
            id=2,
            account_id=1,
            ticker_id=2,
            trade_id=21,
            transaction_date=date(2026, 5, 13),
            shares=-13,
            cost_per_share=Decimal("484.75"),
            total_amount=Decimal("-6301.75"),
        ),
    ]
    issues = walk_cost_basis(sale_rows)
    assert not any("implausible" in i.detail for i in issues)

    assign_after_callaway = [
        CostBasis(
            id=1,
            account_id=1,
            ticker_id=3,
            trade_id=30,
            transaction_date=date(2026, 4, 20),
            shares=638,
            cost_per_share=Decimal("50"),
            total_amount=Decimal("31900"),
        ),
        CostBasis(
            id=2,
            account_id=1,
            ticker_id=3,
            trade_id=31,
            transaction_date=date(2026, 5, 15),
            shares=-600,
            cost_per_share=Decimal("58"),
            total_amount=Decimal("-34800"),
            description="Assigned (called away): RULE ONE CALL OXY 6 contracts @ 58.00",
        ),
        CostBasis(
            id=3,
            account_id=1,
            ticker_id=3,
            trade_id=32,
            transaction_date=date(2026, 6, 18),
            shares=100,
            cost_per_share=Decimal("51"),
            total_amount=Decimal("5100"),
            description="Assigned (acquired): ROCT PUT OXY 1 contracts @ 51.00",
        ),
    ]
    issues = walk_cost_basis(assign_after_callaway)
    assert not any("implausible" in i.detail for i in issues)


def test_reconcile_premium_mismatch():
    computed = {("Rule 1", 2025): Decimal("100.00")}
    expected = [PremiumTotal("Rule 1", 2025, Decimal("90.00"))]
    issues = reconcile_premium(computed, expected)
    assert len(issues) == 1
    assert issues[0].check == "premium"


def test_reconcile_bankroll_empty_table_with_expected():
    account = Account(
        id=1,
        account_name="Rule 1",
        account_type="INVESTMENT",
        start_date=date(2020, 1, 1),
        starting_balance=Decimal("0"),
    )
    issues = reconcile_bankroll(
        accounts={"Rule 1": account},
        bankroll_by_account={},
        expected=[BankrollYearStart("Rule 1", 2026, Decimal("50000"))],
    )
    assert any("empty" in i.detail for i in issues)


def test_load_expected_json(tmp_path):
    path = tmp_path / "expected.json"
    path.write_text(
        json.dumps(
            {
                "premium": [{"account": "Rule 1", "year": 2025, "total": "12.50"}],
                "bankroll_year_start": [{"account": "Roth", "year": 2026, "balance": "1000"}],
            }
        )
    )
    expected = load_expected(path)
    assert expected.premium[0].total == Decimal("12.50")
    assert expected.bankroll_year_start[0].account_name == "Roth"


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _register_now(dbapi_connection: sqlite3.Connection, _):
        dbapi_connection.create_function("now", 0, lambda: "2026-01-01 00:00:00")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_run_validation_flags_needs_review_and_duplicates(session_factory):
    with session_factory() as session:
        account = Account(
            account_name="Rule 1", account_type="INVESTMENT", start_date=date(2020, 1, 1)
        )
        ticker = Ticker(ticker="SLV")
        ttype = TradeType(
            type_name="ROCT PUT",
            category="OPTIONS",
            is_credit=True,
            requires_expiration=True,
            requires_strike=True,
            requires_contracts=True,
        )
        session.add_all([account, ticker, ttype])
        session.flush()
        shared = dict(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker="SLV",
            trade_type_id=ttype.id,
            trade_type="ROCT PUT",
            date_trade_open=date(2026, 1, 2),
            expiration_date=date(2026, 1, 9),
            strike_price=Decimal("55.5"),
            credit_debit=Decimal("0.22"),
            commission_per_share=Decimal("0.0041"),
            net_credit_per_share=Decimal("0.2159"),
            risk_capital_per_share=Decimal("55.2841"),
            margin_percent=Decimal("1"),
            days_to_expiration=7,
            num_of_contracts=2,
        )
        session.add(Trade(**shared, needs_review=True))
        session.add(Trade(**shared, needs_review=False))
        session.add(
            Bankroll(
                account_id=account.id,
                transaction_date=date(2025, 6, 1),
                transaction_amount=Decimal("1000"),
            )
        )
        session.commit()
        report = run_validation(
            session,
            ExpectedTotals(
                premium=[PremiumTotal("Rule 1", 2026, Decimal("99999"))],
                bankroll_year_start=[BankrollYearStart("Rule 1", 2026, Decimal("0"))],
            ),
        )

    checks = {i.check for i in report.issues}
    assert "needs_review" in checks
    assert "duplicates" in checks
    assert "premium" in checks
    assert "bankroll" in checks
    assert not report.ok
