"""Tests for GET /api/positions/summary."""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import positions
from app.db import get_db
from app.models import Account, Base, CostBasis, Ticker, Trade, TradeType


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _register_now(dbapi_connection: sqlite3.Connection, _):
        dbapi_connection.create_function("now", 0, lambda: "2026-01-01 00:00:00")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    with factory() as seed:
        account = Account(
            account_name="Rule 1", account_type="INVESTMENT", start_date=date(2020, 1, 1)
        )
        seed.add(account)
        roct_put = TradeType(
            type_name="ROCT PUT",
            category="OPTIONS",
            is_credit=True,
            requires_expiration=True,
            requires_strike=True,
            requires_contracts=True,
        )
        rule_one_put = TradeType(
            type_name="RULE ONE PUT",
            category="OPTIONS",
            is_credit=True,
            requires_expiration=True,
            requires_strike=True,
            requires_contracts=True,
        )
        bto = TradeType(type_name="BTO", category="STOCK", is_credit=False, requires_shares=True)
        seed.add_all([roct_put, rule_one_put, bto])
        ticker = Ticker(ticker="ADBE")
        seed.add(ticker)
        seed.flush()

        # A trading-shares assignment (ROCT PUT).
        roct_trade = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker="ADBE",
            trade_type_id=roct_put.id,
            trade_type="ROCT PUT",
            date_trade_open=date(2026, 1, 1),
            expiration_date=date(2026, 1, 16),
            num_of_contracts=1,
            strike_price=Decimal("280.00"),
            credit_debit=Decimal("2.00"),
            net_credit_per_share=Decimal("2.00"),
            commission_per_share=Decimal("0.0041"),
        )
        # A long-term-shares assignment (RULE ONE PUT).
        rule_one_trade = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker="ADBE",
            trade_type_id=rule_one_put.id,
            trade_type="RULE ONE PUT",
            date_trade_open=date(2026, 2, 1),
            expiration_date=date(2026, 2, 20),
            num_of_contracts=1,
            strike_price=Decimal("300.00"),
            credit_debit=Decimal("3.00"),
            net_credit_per_share=Decimal("3.00"),
            commission_per_share=Decimal("0.0041"),
        )
        # A still-open put — no premium collected until it's actually closed?
        # No: premium is collected at open, so this still counts. It has no
        # cost_basis row though, since it hasn't been assigned.
        still_open_trade = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker="ADBE",
            trade_type_id=roct_put.id,
            trade_type="ROCT PUT",
            date_trade_open=date(2026, 3, 1),
            expiration_date=date(2026, 3, 20),
            num_of_contracts=1,
            strike_price=Decimal("290.00"),
            credit_debit=Decimal("1.50"),
            net_credit_per_share=Decimal("1.50"),
            commission_per_share=Decimal("0.0041"),
        )
        # A plain stock buy.
        bto_trade = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker="ADBE",
            trade_type_id=bto.id,
            trade_type="BTO",
            date_trade_open=date(2026, 4, 1),
            num_of_shares=10,
            price_per_share=Decimal("310.00"),
            credit_debit=Decimal("3100.00"),
            commission_per_share=Decimal("0"),
        )
        seed.add_all([roct_trade, rule_one_trade, still_open_trade, bto_trade])
        seed.flush()

        seed.add_all(
            [
                CostBasis(
                    account_id=account.id,
                    ticker_id=ticker.id,
                    trade_id=roct_trade.id,
                    transaction_date=date(2026, 1, 16),
                    shares=100,
                    cost_per_share=Decimal("280.00"),
                    total_amount=Decimal("28000.00"),
                ),
                CostBasis(
                    account_id=account.id,
                    ticker_id=ticker.id,
                    trade_id=rule_one_trade.id,
                    transaction_date=date(2026, 2, 20),
                    shares=100,
                    cost_per_share=Decimal("300.00"),
                    total_amount=Decimal("30000.00"),
                ),
                CostBasis(
                    account_id=account.id,
                    ticker_id=ticker.id,
                    trade_id=bto_trade.id,
                    transaction_date=date(2026, 4, 1),
                    shares=10,
                    cost_per_share=Decimal("310.00"),
                    total_amount=Decimal("3100.00"),
                ),
            ]
        )
        seed.commit()

    test_app = FastAPI()
    test_app.include_router(positions.router)

    def _override_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    test_app.dependency_overrides[get_db] = _override_get_db
    return TestClient(test_app)


def test_position_summary_splits_trading_and_long_term(client):
    response = client.get(
        "/api/positions/summary", params={"ticker": "adbe", "account": "Rule 1"}
    )
    assert response.status_code == 200
    body = response.json()

    assert body["ticker"] == "ADBE"
    assert body["account_name"] == "Rule 1"

    # trading = ROCT assignment only (100 sh @ 280)
    assert Decimal(body["trading"]["shares"]) == Decimal("100")
    assert Decimal(body["trading"]["cost_basis"]) == Decimal("28000.00")
    assert Decimal(body["trading"]["avg_cost_per_share"]) == Decimal("280.00")

    # long_term = RULE ONE assignment (100 sh @ 300) + BTO (10 sh @ 310)
    assert Decimal(body["long_term"]["shares"]) == Decimal("110")
    assert Decimal(body["long_term"]["cost_basis"]) == Decimal("33100.00")

    assert Decimal(body["total"]["shares"]) == Decimal("210")
    assert Decimal(body["total"]["cost_basis"]) == Decimal("61100.00")


def test_position_summary_total_premium_includes_all_option_trades_any_status(client):
    response = client.get(
        "/api/positions/summary", params={"ticker": "ADBE", "account": "Rule 1"}
    )
    body = response.json()

    # (2.00 + 3.00 + 1.50) * 1 contract * 100 = 650, including the still-open
    # trade and both assigned trades — no status/date filtering.
    assert Decimal(body["total_premium_collected"]) == Decimal("650.00")


def test_position_summary_unknown_ticker_404s(client):
    response = client.get(
        "/api/positions/summary", params={"ticker": "ZZZZ", "account": "Rule 1"}
    )
    assert response.status_code == 404


def test_position_summary_unknown_account_404s(client):
    response = client.get(
        "/api/positions/summary", params={"ticker": "ADBE", "account": "Nonexistent"}
    )
    assert response.status_code == 404
