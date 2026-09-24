"""Tests for GET /api/analytics/summary and GET /api/analytics/premium-timeseries.

Same throwaway-SQLite-with-hand-recreated-views approach as
`test_trades_api.py`; seeds a handful of known trades and asserts exact
expected numbers, per the plan's "mirror test_calculators_api.py's pattern"
guidance.
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import analytics
from app.db import get_db
from app.models import Account, Base, Ticker, Trade, TradeEvent, TradeType

_CREATE_CURRENT_STATUS_VIEW = """
    CREATE VIEW v_trade_current_status AS
    SELECT
        t.id AS trade_id,
        t.account_id,
        t.ticker_id,
        CASE
            WHEN EXISTS (SELECT 1 FROM trade_events e WHERE e.trade_id = t.id AND e.event_type IN ('CLOSE','EXPIRE','ASSIGN'))
            THEN 'closed' ELSE 'open'
        END AS trade_status,
        (SELECT event_date FROM trade_events e WHERE e.trade_id = t.id AND e.event_type IN ('CLOSE','EXPIRE','ASSIGN') ORDER BY event_date DESC LIMIT 1) AS date_trade_closed,
        (SELECT closing_debit FROM trade_events e WHERE e.trade_id = t.id AND e.event_type IN ('CLOSE','EXPIRE','ASSIGN') ORDER BY event_date DESC LIMIT 1) AS closing_debit,
        (SELECT event_date FROM trade_events e WHERE e.trade_id = t.id AND e.event_type = 'ROLL' ORDER BY event_date DESC LIMIT 1) AS date_trade_rolled
    FROM trades t
"""


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
    with engine.connect() as conn:
        conn.execute(text(_CREATE_CURRENT_STATUS_VIEW))
        conn.commit()

    factory = sessionmaker(bind=engine)

    with factory() as seed:
        account = Account(
            account_name="Rule 1", account_type="INVESTMENT", start_date=date(2020, 1, 1)
        )
        seed.add(account)
        put_type = TradeType(
            type_name="ROCT PUT",
            category="OPTIONS",
            is_credit=True,
            requires_expiration=True,
            requires_strike=True,
            requires_contracts=True,
        )
        stock_type = TradeType(type_name="BTO", category="STOCK", is_credit=False)
        seed.add_all([put_type, stock_type])
        ticker = Ticker(ticker="SLV")
        seed.add(ticker)
        seed.flush()

        # Open put, opened this week (2026-01-01 is a Thursday -> week is
        # Dec 29, 2025 - Jan 4, 2026), $0.30/share credit x 2 contracts = $60.
        open_trade = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker="SLV",
            trade_type_id=put_type.id,
            trade_type="ROCT PUT",
            date_trade_open=date(2026, 1, 1),
            expiration_date=date(2026, 1, 5),
            num_of_contracts=2,
            strike_price=Decimal("50"),
            credit_debit=Decimal("0.30"),
            net_credit_per_share=Decimal("0.30"),
            risk_capital_per_share=Decimal("49.70"),
            commission_per_share=Decimal("0.0041"),
            arorc=Decimal("0.40"),
        )
        # Closed put, opened earlier in the year - premium counts toward
        # YTD average but not "this week".
        closed_trade = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker="SLV",
            trade_type_id=put_type.id,
            trade_type="ROCT PUT",
            date_trade_open=date(2025, 12, 1),
            expiration_date=date(2025, 12, 8),
            num_of_contracts=1,
            strike_price=Decimal("50"),
            credit_debit=Decimal("0.50"),
            net_credit_per_share=Decimal("0.50"),
            risk_capital_per_share=Decimal("49.50"),
            commission_per_share=Decimal("0.0041"),
        )
        # Plain stock buy - must not count as premium.
        stock_trade = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker="SLV",
            trade_type_id=stock_type.id,
            trade_type="BTO",
            date_trade_open=date(2026, 1, 1),
            num_of_shares=10,
            credit_debit=Decimal("500.00"),
            commission_per_share=Decimal("0"),
        )
        # Still "open" per the event log, but its expiration date has
        # already passed — the needs-review signal.
        stale_trade = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker="SLV",
            trade_type_id=put_type.id,
            trade_type="ROCT PUT",
            date_trade_open=date(2025, 12, 1),
            expiration_date=date(2025, 12, 15),
            num_of_contracts=1,
            strike_price=Decimal("50"),
            credit_debit=Decimal("0.10"),
            net_credit_per_share=Decimal("0.10"),
            commission_per_share=Decimal("0.0041"),
        )
        # Still live in v_trade_current_status, but the table's Open chip
        # (display_status) treats ROLL as not open.
        rolled_trade = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker="SLV",
            trade_type_id=put_type.id,
            trade_type="ROCT PUT",
            date_trade_open=date(2025, 11, 1),
            expiration_date=date(2026, 6, 1),
            num_of_contracts=1,
            strike_price=Decimal("50"),
            credit_debit=Decimal("0.20"),
            net_credit_per_share=Decimal("0.20"),
            commission_per_share=Decimal("0.0041"),
            arorc=Decimal("0.99"),
        )
        seed.add_all([open_trade, closed_trade, stock_trade, stale_trade, rolled_trade])
        seed.flush()

        seed.add(TradeEvent(trade_id=open_trade.id, event_type="OPEN", event_date=date(2026, 1, 1)))
        seed.add(
            TradeEvent(trade_id=closed_trade.id, event_type="OPEN", event_date=date(2025, 12, 1))
        )
        seed.add(
            TradeEvent(
                trade_id=closed_trade.id, event_type="EXPIRE", event_date=date(2025, 12, 8)
            )
        )
        seed.add(TradeEvent(trade_id=stock_trade.id, event_type="OPEN", event_date=date(2026, 1, 1)))
        seed.add(
            TradeEvent(trade_id=stale_trade.id, event_type="OPEN", event_date=date(2025, 12, 1))
        )
        seed.add(
            TradeEvent(trade_id=rolled_trade.id, event_type="OPEN", event_date=date(2025, 11, 1))
        )
        seed.add(
            TradeEvent(trade_id=rolled_trade.id, event_type="ROLL", event_date=date(2025, 11, 15))
        )
        seed.commit()

        ids = {
            "open": open_trade.id,
            "closed": closed_trade.id,
            "stock": stock_trade.id,
            "stale": stale_trade.id,
            "rolled": rolled_trade.id,
        }

    test_app = FastAPI()
    test_app.include_router(analytics.router)

    def _override_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    test_app.dependency_overrides[get_db] = _override_get_db
    test_client = TestClient(test_app)
    test_client.trade_ids = ids  # type: ignore[attr-defined]
    return test_client


def test_summary_open_trades_count(client):
    response = client.get("/api/analytics/summary", params={"as_of": "2026-01-01"})
    assert response.status_code == 200
    body = response.json()

    # open put + stale. BTO is stock; rolled is live but not display-open.
    assert body["open_trades_count"] == 2
    assert Decimal(body["avg_arorc_open"]) == Decimal("0.40")


def test_summary_open_trades_count_respects_open_date_range(client):
    response = client.get(
        "/api/analytics/summary",
        params={"as_of": "2026-01-01", "date_from": "2026-01-01", "date_to": "2026-01-01"},
    )
    assert response.json()["open_trades_count"] == 1


def test_summary_premium_this_week_excludes_stock_and_older_trades(client):
    response = client.get("/api/analytics/summary", params={"as_of": "2026-01-01"})
    body = response.json()
    # Only the open put's premium (0.30 * 2 * 100 = 60) falls in this week.
    assert Decimal(body["premium_this_week"]) == Decimal("60")


def test_summary_avg_weekly_premium_ytd_includes_only_current_year(client):
    response = client.get("/api/analytics/summary", params={"as_of": "2026-01-01"})
    body = response.json()
    # YTD = Jan 1 2026 - Jan 1 2026 (1 day = 1/7 week); only open put's 60
    # premium falls in-year (closed trade was opened in Dec 2025).
    expected = Decimal("60") / (Decimal("1") / Decimal("7"))
    assert Decimal(body["avg_weekly_premium_ytd"]) == expected


def test_summary_upcoming_expirations_7d(client):
    response = client.get("/api/analytics/summary", params={"as_of": "2026-01-01"})
    body = response.json()
    assert body["upcoming_expirations_7d_count"] == 1
    assert body["upcoming_expirations_7d"][0]["id"] == client.trade_ids["open"]
    assert body["upcoming_expirations_7d"][0]["expiration_date"] == "2026-01-05"


def test_summary_needs_review_counts_expired_but_still_open(client):
    response = client.get("/api/analytics/summary", params={"as_of": "2026-01-01"})
    # Only the stale trade is open with an expiration date already in the past.
    assert response.json()["needs_review_count"] == 1


def test_summary_total_premium_ytd(client):
    response = client.get("/api/analytics/summary", params={"as_of": "2026-01-01"})
    body = response.json()
    # Only the open put's premium (0.30 * 2 * 100 = 60) was opened in 2026;
    # the closed and stale trades were opened in 2025.
    assert Decimal(body["total_premium_ytd"]) == Decimal("60")


def test_premium_timeseries_weekly_buckets(client):
    response = client.get(
        "/api/analytics/premium-timeseries",
        params={"start": "2025-12-29", "end": "2026-01-04", "bucket": "week"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["bucket"] == "week"
    assert len(body["items"]) == 1
    assert body["items"][0]["period_start"] == "2025-12-29"
    assert body["items"][0]["period_end"] == "2026-01-04"
    assert Decimal(body["items"][0]["premium_total"]) == Decimal("60")
    assert body["items"][0]["trade_count"] == 1


def test_premium_timeseries_invalid_bucket_rejected(client):
    response = client.get(
        "/api/analytics/premium-timeseries",
        params={"start": "2026-01-01", "end": "2026-01-31", "bucket": "year"},
    )
    assert response.status_code == 400


def test_premium_timeseries_end_before_start_rejected(client):
    response = client.get(
        "/api/analytics/premium-timeseries",
        params={"start": "2026-01-31", "end": "2026-01-01"},
    )
    assert response.status_code == 400
