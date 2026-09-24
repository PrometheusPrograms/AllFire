"""Tests for scripts.manual_entry loader."""

import sqlite3
from datetime import date

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.models import Account, Base, CashFlow, CostBasis, Trade, TradeEvent, TradeType
from scripts.manual_entry import load_manual_records


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _register_now(dbapi_connection: sqlite3.Connection, _):
        dbapi_connection.create_function("now", 0, lambda: "2026-01-01 00:00:00")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as seed:
        seed.add(
            Account(
                account_name="Rule 1",
                account_type="INVESTMENT",
                start_date=date(2020, 1, 1),
            )
        )
        seed.add(
            TradeType(
                type_name="BTO",
                category="STOCK",
                is_credit=False,
                requires_shares=True,
            )
        )
        seed.add(
            TradeType(
                type_name="STC",
                category="STOCK",
                is_credit=False,
                requires_shares=True,
            )
        )
        seed.commit()
    return factory


def test_loads_stc_with_negative_cost_basis(session_factory):
    with session_factory() as session:
        result = load_manual_records(
            session,
            [
                {
                    "kind": "stc",
                    "account": "Rule 1",
                    "ticker": "ADBE",
                    "date": "2025-06-01",
                    "shares": 100,
                    "price": "12.50",
                    "commission_per_share": "0.01",
                }
            ],
        )
        session.commit()
        assert result.created == 1
        trade = session.scalar(select(Trade))
        assert trade is not None
        assert trade.trade_type == "STC"
        assert trade.import_batch == "manual"
        event = session.scalar(select(TradeEvent))
        assert event is not None
        assert event.event_type == "OPEN"
        basis = session.scalar(select(CostBasis))
        assert basis is not None
        assert basis.shares == -100
        assert basis.total_amount < 0


def test_loads_dividend_and_skips_duplicate(session_factory):
    row = {
        "kind": "dividend",
        "account": "Rule 1",
        "ticker": "AAPL",
        "date": "2025-03-15",
        "amount": "12.34",
        "description": "hand entered",
    }
    with session_factory() as session:
        first = load_manual_records(session, [row])
        session.commit()
        assert first.created == 1
        second = load_manual_records(session, [row])
        session.commit()
        assert second.skipped == 1
        assert len(session.scalars(select(CashFlow)).all()) == 1


def test_unknown_kind_is_an_error(session_factory):
    with session_factory() as session:
        result = load_manual_records(session, [{"kind": "journal", "account": "Rule 1"}])
        assert result.errors
        assert result.created == 0
