"""Tests for GET /api/trades and GET /api/trades/{id}.

Builds a throwaway SQLite DB with `Base.metadata.create_all` plus the two
views (`v_trade_current_status`, `v_cost_basis_running`) recreated verbatim
from `alembic/versions/d5e684ca66b6_initial_schema.py`, since Alembic
manages those as raw SQL rather than ORM models. Never touches the real
database either way.
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

from app.api import trades
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

_CREATE_COST_BASIS_VIEW = """
    CREATE VIEW v_cost_basis_running AS
    SELECT
        cb.*,
        SUM(cb.total_amount) OVER (
            PARTITION BY cb.account_id, cb.ticker_id ORDER BY cb.transaction_date, cb.id
        ) AS running_basis,
        SUM(cb.shares) OVER (
            PARTITION BY cb.account_id, cb.ticker_id ORDER BY cb.transaction_date, cb.id
        ) AS running_shares
    FROM cost_basis cb
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
        conn.execute(text(_CREATE_COST_BASIS_VIEW))
        conn.commit()

    factory = sessionmaker(bind=engine)

    with factory() as seed:
        account = Account(account_name="Rule 1", account_type="INVESTMENT", start_date=date(2020, 1, 1))
        seed.add(account)
        trade_type = TradeType(
            type_name="ROCT PUT",
            category="OPTIONS",
            is_credit=True,
            requires_expiration=True,
            requires_strike=True,
            requires_contracts=True,
        )
        seed.add(trade_type)
        ticker = Ticker(ticker="SLV")
        seed.add(ticker)
        seed.flush()

        closed_trade = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker="SLV",
            trade_type_id=trade_type.id,
            trade_type="ROCT PUT",
            date_trade_open=date(2026, 1, 2),
            expiration_date=date(2026, 1, 9),
            strike_price=Decimal("55.5"),
            price_per_share=Decimal("85.04"),
            credit_debit=Decimal("0.22"),
            commission_per_share=Decimal("0.0041"),
            arorc=Decimal("0.2036"),
        )
        open_trade = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker="SLV",
            trade_type_id=trade_type.id,
            trade_type="ROCT PUT",
            date_trade_open=date(2026, 2, 1),
            expiration_date=date(2026, 2, 8),
            strike_price=Decimal("50"),
            credit_debit=Decimal("0.30"),
            commission_per_share=Decimal("0.0041"),
        )
        seed.add_all([closed_trade, open_trade])
        seed.flush()

        seed.add(
            TradeEvent(
                trade_id=closed_trade.id, event_type="OPEN", event_date=date(2026, 1, 2)
            )
        )
        seed.add(
            TradeEvent(
                trade_id=closed_trade.id,
                event_type="EXPIRE",
                event_date=date(2026, 1, 9),
                notes="Imported RESULT='EXPIRED'",
            )
        )
        seed.add(
            TradeEvent(trade_id=open_trade.id, event_type="OPEN", event_date=date(2026, 2, 1))
        )
        seed.commit()

        ids = {"closed": closed_trade.id, "open": open_trade.id}

    test_app = FastAPI()
    test_app.include_router(trades.router)

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


def test_list_trades_returns_all_by_default(client):
    response = client.get("/api/trades")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2


def test_list_trades_filters_by_status(client):
    response = client.get("/api/trades", params={"status": "closed"})
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == client.trade_ids["closed"]
    assert body["items"][0]["trade_status"] == "closed"
    assert body["items"][0]["date_trade_closed"] == "2026-01-09"


def test_list_trades_filters_by_ticker_and_account(client):
    response = client.get("/api/trades", params={"ticker": "slv", "account": "Rule 1"})
    assert response.json()["total"] == 2

    response = client.get("/api/trades", params={"ticker": "NOPE"})
    assert response.json()["total"] == 0


def test_list_trades_pagination(client):
    response = client.get("/api/trades", params={"limit": 1, "offset": 0})
    body = response.json()
    assert body["total"] == 2
    assert len(body["items"]) == 1
    # newest date_trade_open first
    assert body["items"][0]["id"] == client.trade_ids["open"]


def test_invalid_status_is_rejected(client):
    response = client.get("/api/trades", params={"status": "sideways"})
    assert response.status_code == 400


def test_list_trades_display_status_expired(client):
    response = client.get("/api/trades", params={"display_status": "expired"})
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == client.trade_ids["closed"]
    assert body["items"][0]["display_status"] == "expired"


def test_list_trades_display_status_open(client):
    response = client.get("/api/trades", params={"display_status": "open"})
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == client.trade_ids["open"]
    assert body["items"][0]["display_status"] == "open"


def test_invalid_display_status_is_rejected(client):
    response = client.get("/api/trades", params={"display_status": "sideways"})
    assert response.status_code == 400


def test_list_trades_needs_review_computed_filters_expired_but_open(client):
    # open_trade's expiration_date (2026-02-08) is after as_of, so it's not
    # a needs-review case even though its display_status is "open".
    response = client.get(
        "/api/trades", params={"needs_review_computed": True, "as_of": "2026-02-09"}
    )
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == client.trade_ids["open"]
    assert body["items"][0]["display_status"] == "open"

    response = client.get(
        "/api/trades", params={"needs_review_computed": True, "as_of": "2026-01-01"}
    )
    assert response.json()["total"] == 0


def test_list_trades_filters_by_date_range(client):
    response = client.get(
        "/api/trades", params={"date_from": "2026-01-15", "date_to": "2026-02-28"}
    )
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == client.trade_ids["open"]

    response = client.get("/api/trades", params={"date_from": "2026-03-01"})
    assert response.json()["total"] == 0


def test_get_trade_detail_includes_event_timeline(client):
    trade_id = client.trade_ids["closed"]
    response = client.get(f"/api/trades/{trade_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["trade_status"] == "closed"
    assert [e["event_type"] for e in body["events"]] == ["OPEN", "EXPIRE"]
    assert body["running_basis"] is None  # no cost_basis rows in this scenario


def test_get_trade_detail_404_for_unknown_id(client):
    response = client.get("/api/trades/999999")
    assert response.status_code == 404
