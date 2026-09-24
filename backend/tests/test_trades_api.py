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


def _memory_engine():
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
    return engine, sessionmaker(bind=engine)


def _seed_refs(seed):
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
    stock_type = TradeType(
        type_name="BTO",
        category="STOCK",
        is_credit=False,
        requires_shares=True,
    )
    seed.add(stock_type)
    ticker = Ticker(ticker="SLV")
    seed.add(ticker)
    seed.flush()
    return account, trade_type, stock_type, ticker


def _trades_client(factory, ids):
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


@pytest.fixture
def client():
    engine, factory = _memory_engine()

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
        stock_type = TradeType(
            type_name="BTO",
            category="STOCK",
            is_credit=False,
            requires_shares=True,
        )
        seed.add(stock_type)
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

        stock_trade = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker="SLV",
            trade_type_id=stock_type.id,
            trade_type="BTO",
            date_trade_open=date(2025, 6, 1),
            num_of_shares=10,
            price_per_share=Decimal("20"),
            credit_debit=Decimal("20"),
            commission_per_share=Decimal("0"),
        )
        seed.add(stock_trade)
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
        seed.add(
            TradeEvent(trade_id=stock_trade.id, event_type="OPEN", event_date=date(2025, 6, 1))
        )
        seed.commit()

        ids = {"closed": closed_trade.id, "open": open_trade.id, "stock": stock_trade.id}

    return _trades_client(factory, ids)


@pytest.fixture
def client_with_roll():
    engine, factory = _memory_engine()
    with factory() as seed:
        account, trade_type, _, ticker = _seed_refs(seed)
        first = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker="SLV",
            trade_type_id=trade_type.id,
            trade_type="ROCT PUT",
            date_trade_open=date(2026, 1, 2),
            expiration_date=date(2026, 1, 9),
            days_to_expiration=7,
            strike_price=Decimal("55.50"),
            num_of_contracts=1,
            num_of_shares=100,
            credit_debit=Decimal("0.22"),
            net_credit_per_share=Decimal("0.22"),
            risk_capital_per_share=Decimal("55.28"),
            commission_per_share=Decimal("0.0041"),
        )
        second = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker="SLV",
            trade_type_id=trade_type.id,
            trade_type="ROCT PUT",
            trade_parent_id=None,
            date_trade_open=date(2026, 1, 9),
            expiration_date=date(2026, 1, 16),
            days_to_expiration=7,
            strike_price=Decimal("54.00"),
            num_of_contracts=1,
            num_of_shares=100,
            credit_debit=Decimal("0.18"),
            net_credit_per_share=Decimal("0.18"),
            risk_capital_per_share=Decimal("53.82"),
            commission_per_share=Decimal("0.0041"),
        )
        seed.add_all([first, second])
        seed.flush()
        second.trade_parent_id = first.id
        seed.add(TradeEvent(trade_id=first.id, event_type="OPEN", event_date=date(2026, 1, 2)))
        seed.add(TradeEvent(trade_id=first.id, event_type="ROLL", event_date=date(2026, 1, 9)))
        seed.add(TradeEvent(trade_id=second.id, event_type="OPEN", event_date=date(2026, 1, 9)))
        seed.add(TradeEvent(trade_id=second.id, event_type="EXPIRE", event_date=date(2026, 1, 16)))
        seed.commit()
        ids = {"root": first.id, "tip": second.id}
    return _trades_client(factory, ids)


def test_list_trades_returns_all_by_default(client):
    response = client.get("/api/trades")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3


def test_list_trades_filters_by_status(client):
    response = client.get("/api/trades", params={"status": "closed"})
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == client.trade_ids["closed"]
    assert body["items"][0]["trade_status"] == "closed"
    assert body["items"][0]["date_trade_closed"] == "2026-01-09"


def test_list_trades_filters_by_ticker_and_account(client):
    response = client.get("/api/trades", params={"ticker": "slv", "account": "Rule 1"})
    assert response.json()["total"] == 3

    response = client.get("/api/trades", params={"ticker": "NOPE"})
    assert response.json()["total"] == 0


def test_list_trades_pagination(client):
    response = client.get("/api/trades", params={"limit": 1, "offset": 0})
    body = response.json()
    assert body["total"] == 3
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


def test_list_trades_bto_has_no_display_status(client):
    response = client.get("/api/trades", params={"trade_type": "BTO"})
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == client.trade_ids["stock"]
    assert body["items"][0]["display_status"] is None

    response = client.get("/api/trades", params={"display_status": "open"})
    assert all(item["trade_type"] != "BTO" for item in response.json()["items"])


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


def test_list_collapses_roll_chain_to_one_row(client_with_roll):
    response = client_with_roll.get("/api/trades")
    body = response.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == client_with_roll.trade_ids["root"]
    assert item["roll_legs"] == 2
    assert item["strike_path"] == "55.50 → 54.00"
    assert item["display_status"] == "expired"
    assert item["date_trade_open"] == "2026-01-02"
    assert item["expiration_date"] == "2026-01-16"
    assert item["chain_trade_ids"] == [
        client_with_roll.trade_ids["root"],
        client_with_roll.trade_ids["tip"],
    ]
    # Both legs' premium: 0.22*1*100 + 0.18*1*100
    assert Decimal(item["premium_collected"]) == Decimal("40")
    assert Decimal(item["chain_net_credit_per_share"]) == Decimal("0.40")
    assert item["chain_dte"] == 14
    assert item["chain_arorc"] is not None


def test_list_date_range_includes_chain_if_any_leg_matches(client_with_roll):
    response = client_with_roll.get(
        "/api/trades", params={"date_from": "2026-01-09", "date_to": "2026-01-09"}
    )
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == client_with_roll.trade_ids["root"]


def test_get_trade_chain_timeline_links_each_leg(client_with_roll):
    root_id = client_with_roll.trade_ids["root"]
    tip_id = client_with_roll.trade_ids["tip"]
    response = client_with_roll.get(f"/api/trades/{root_id}")
    body = response.json()
    assert body["chain_root_id"] == root_id
    assert [e["event_type"] for e in body["chain_events"]] == [
        "OPEN",
        "ROLL",
        "OPEN",
        "EXPIRE",
    ]
    assert [e["trade_id"] for e in body["chain_events"]] == [
        root_id,
        root_id,
        tip_id,
        tip_id,
    ]
    assert len(body["chain_legs"]) == 2
    roll = next(e for e in body["chain_events"] if e["event_type"] == "ROLL")
    assert roll["next_trade_id"] == tip_id
    assert roll["roll_kind"] == "diagonal"
    assert roll["option_right"] == "PUT"
    assert Decimal(str(roll["to_strike"])) == Decimal("54.00")
    assert roll["to_expiration"] == "2026-01-16"
    assert body["chain_arorc"] is not None
    root_leg, tip_leg = body["chain_legs"]
    assert root_leg["result"] == "ROLL"
    assert root_leg["result_date"] == "2026-01-09"
    assert root_leg["price_per_share"] is None
    assert Decimal(str(root_leg["credit_debit"])) == Decimal("0.22")
    assert Decimal(str(root_leg["strike_price"])) == Decimal("55.50")
    assert root_leg["days_to_expiration"] == 7
    assert root_leg["long_strike"] is None
    assert tip_leg["result"] == "EXPIRED"
    assert tip_leg["result_date"] == "2026-01-16"
    assert Decimal(str(tip_leg["result_net_credit"])) == Decimal("18")
