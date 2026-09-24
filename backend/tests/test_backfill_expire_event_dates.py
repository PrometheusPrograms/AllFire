"""Tests for EXPIRE event_date backfill."""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models import Account, Base, Ticker, Trade, TradeEvent, TradeType
from scripts.backfill_expire_event_dates import backfill_expire_dates


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
                type_name="ROCT PUT",
                category="OPTIONS",
                is_credit=True,
                requires_expiration=True,
                requires_strike=True,
                requires_contracts=True,
            )
        )
        seed.add(Ticker(ticker="LULU"))
        seed.commit()
    return factory


def test_backfill_sets_expire_to_expiration_date(session_factory):
    with session_factory() as session:
        account = session.query(Account).one()
        ticker = session.query(Ticker).one()
        ttype = session.query(TradeType).one()
        trade = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker="LULU",
            trade_type_id=ttype.id,
            trade_type="ROCT PUT",
            date_trade_open=date(2024, 9, 4),
            expiration_date=date(2024, 9, 13),
            num_of_contracts=1,
            strike_price=Decimal("245.00"),
            credit_debit=Decimal("2.92"),
            commission_per_share=Decimal("0"),
        )
        session.add(trade)
        session.flush()
        session.add(
            TradeEvent(trade_id=trade.id, event_type="OPEN", event_date=date(2024, 9, 4))
        )
        expire = TradeEvent(
            trade_id=trade.id, event_type="EXPIRE", event_date=date(2024, 9, 4)
        )
        session.add(expire)
        session.commit()
        expire_id = expire.id

    with session_factory() as session:
        changed = backfill_expire_dates(session, dry_run=False)
        session.commit()
        assert len(changed) == 1
        event = session.get(TradeEvent, expire_id)
        assert event.event_date == date(2024, 9, 13)
        open_event = session.query(TradeEvent).filter_by(event_type="OPEN").one()
        assert open_event.event_date == date(2024, 9, 4)
