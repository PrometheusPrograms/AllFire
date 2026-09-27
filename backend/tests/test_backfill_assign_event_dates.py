"""Tests for ASSIGN event_date backfill."""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models import Account, Base, CostBasis, Ticker, Trade, TradeEvent, TradeType
from scripts.backfill_assign_event_dates import backfill_assign_dates


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
        seed.add(Ticker(ticker="GDXY"))
        seed.commit()
    return factory


def _assigned_put(session, *, assign_date: date) -> tuple[int, int]:
    account = session.query(Account).one()
    ticker = session.query(Ticker).one()
    ttype = session.query(TradeType).one()
    trade = Trade(
        account_id=account.id,
        ticker_id=ticker.id,
        ticker="GDXY",
        trade_type_id=ttype.id,
        trade_type="ROCT PUT",
        date_trade_open=date(2025, 5, 16),
        expiration_date=date(2025, 7, 18),
        num_of_contracts=1,
        strike_price=Decimal("15.00"),
        credit_debit=Decimal("0.52"),
        commission_per_share=Decimal("0"),
    )
    session.add(trade)
    session.flush()
    session.add(TradeEvent(trade_id=trade.id, event_type="OPEN", event_date=date(2025, 5, 16)))
    assign = TradeEvent(trade_id=trade.id, event_type="ASSIGN", event_date=assign_date)
    session.add(assign)
    lot = CostBasis(
        account_id=account.id,
        ticker_id=ticker.id,
        trade_id=trade.id,
        transaction_date=assign_date,
        description="Assigned (acquired)",
        shares=100,
        cost_per_share=Decimal("15.00"),
        total_amount=Decimal("1500.00"),
    )
    session.add(lot)
    session.commit()
    return assign.id, lot.id


def test_open_dated_assign_moves_to_expiration_with_its_lot(session_factory):
    with session_factory() as session:
        assign_id, lot_id = _assigned_put(session, assign_date=date(2025, 5, 16))

    with session_factory() as session:
        changed = backfill_assign_dates(session, dry_run=False)
        session.commit()
        assert changed == [(assign_id, date(2025, 5, 16), date(2025, 7, 18))]
        assert session.get(TradeEvent, assign_id).event_date == date(2025, 7, 18)
        assert session.get(CostBasis, lot_id).transaction_date == date(2025, 7, 18)
        open_event = session.query(TradeEvent).filter_by(event_type="OPEN").one()
        assert open_event.event_date == date(2025, 5, 16)


def test_early_assignment_date_is_left_alone(session_factory):
    with session_factory() as session:
        assign_id, lot_id = _assigned_put(session, assign_date=date(2025, 7, 2))

    with session_factory() as session:
        assert backfill_assign_dates(session, dry_run=False) == []
        assert session.get(TradeEvent, assign_id).event_date == date(2025, 7, 2)
        assert session.get(CostBasis, lot_id).transaction_date == date(2025, 7, 2)


def test_dry_run_changes_nothing(session_factory):
    with session_factory() as session:
        assign_id, _ = _assigned_put(session, assign_date=date(2025, 5, 16))

    with session_factory() as session:
        assert len(backfill_assign_dates(session, dry_run=True)) == 1
        session.rollback()
        assert session.get(TradeEvent, assign_id).event_date == date(2025, 5, 16)
