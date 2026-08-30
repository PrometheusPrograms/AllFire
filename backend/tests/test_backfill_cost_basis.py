"""Tests for scripts/backfill_cost_basis.py — DB-touching behavior (sign/math
per trade type, idempotency, RUT-spread exclusion) against a throwaway
in-memory SQLite DB.
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.models import Account, Base, CostBasis, Ticker, Trade, TradeEvent, TradeType
from scripts.backfill_cost_basis import backfill

ZERO = Decimal("0")


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _register_now(dbapi_connection: sqlite3.Connection, _):
        dbapi_connection.create_function("now", 0, lambda: "2026-01-01 00:00:00")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def seeded_session(session_factory):
    with session_factory() as session:
        account = Account(
            account_name="Rule 1", account_type="INVESTMENT", start_date=date(2020, 1, 1)
        )
        ticker = Ticker(ticker="ADBE")
        session.add_all([account, ticker])
        session.flush()

        for name, category, is_credit in [
            ("ROCT PUT", "OPTIONS", True),
            ("ROCT CALL", "OPTIONS", True),
            ("RULE ONE PUT", "OPTIONS", True),
            ("RULE ONE CALL", "OPTIONS", True),
            ("ROCS BULL PUT SPREAD", "OPTIONS", True),
            ("BULL PUT SPREAD", "OPTIONS", True),
            ("BTO", "STOCK", False),
            ("STC", "STOCK", True),
        ]:
            session.add(
                TradeType(
                    type_name=name,
                    category=category,
                    is_credit=is_credit,
                    requires_expiration=category == "OPTIONS",
                    requires_strike=category == "OPTIONS",
                    requires_contracts=category == "OPTIONS",
                    requires_shares=category == "STOCK",
                )
            )
        session.commit()
        yield session


def _trade_type_id(session, name: str) -> int:
    return session.scalar(select(TradeType.id).where(TradeType.type_name == name))


def _account_ticker_ids(session):
    account_id = session.scalar(select(Account.id))
    ticker_id = session.scalar(select(Ticker.id))
    return account_id, ticker_id


def _base_trade(session, **overrides) -> Trade:
    account_id, ticker_id = _account_ticker_ids(session)
    defaults = dict(
        account_id=account_id,
        ticker_id=ticker_id,
        ticker="ADBE",
        date_trade_open=date(2026, 1, 2),
        credit_debit=Decimal("0"),
        commission_per_share=Decimal("0"),
    )
    defaults.update(overrides)
    trade = Trade(**defaults)
    session.add(trade)
    session.flush()
    return trade


def test_bto_creates_positive_share_row(seeded_session):
    session = seeded_session
    trade = _base_trade(
        session,
        trade_type_id=_trade_type_id(session, "BTO"),
        trade_type="BTO",
        num_of_shares=100,
        price_per_share=Decimal("50.00"),
        commission_per_share=Decimal("0.01"),
    )
    session.commit()

    result = backfill(session)
    session.commit()

    assert result.rows_created == 1
    row = session.scalar(select(CostBasis).where(CostBasis.trade_id == trade.id))
    assert row.shares == 100
    assert row.cost_per_share == Decimal("50.00")
    assert row.total_amount == Decimal("5001.00")  # 100 * (50 + 0.01)


def test_stc_creates_negative_share_row(seeded_session):
    session = seeded_session
    trade = _base_trade(
        session,
        trade_type_id=_trade_type_id(session, "STC"),
        trade_type="STC",
        num_of_shares=40,
        price_per_share=Decimal("60.00"),
        commission_per_share=Decimal("0.01"),
    )
    session.commit()

    result = backfill(session)
    session.commit()

    assert result.rows_created == 1
    row = session.scalar(select(CostBasis).where(CostBasis.trade_id == trade.id))
    assert row.shares == -40
    assert row.total_amount == Decimal("-2399.60")  # -(40 * (60 - 0.01))


def test_assigned_put_acquires_shares_at_strike(seeded_session):
    session = seeded_session
    trade = _base_trade(
        session,
        trade_type_id=_trade_type_id(session, "ROCT PUT"),
        trade_type="ROCT PUT",
        num_of_contracts=2,
        strike_price=Decimal("45.00"),
        expiration_date=date(2026, 1, 16),
    )
    session.add(
        TradeEvent(trade_id=trade.id, event_type="ASSIGN", event_date=date(2026, 1, 16))
    )
    session.commit()

    result = backfill(session)
    session.commit()

    assert result.rows_created == 1
    row = session.scalar(select(CostBasis).where(CostBasis.trade_id == trade.id))
    assert row.shares == 200  # 2 contracts * 100
    assert row.cost_per_share == Decimal("45.00")
    assert row.total_amount == Decimal("9000.00")
    assert row.transaction_date == date(2026, 1, 16)  # the ASSIGN event's date


def test_assigned_call_gives_up_shares_at_strike(seeded_session):
    session = seeded_session
    trade = _base_trade(
        session,
        trade_type_id=_trade_type_id(session, "ROCT CALL"),
        trade_type="ROCT CALL",
        num_of_contracts=1,
        strike_price=Decimal("60.00"),
        expiration_date=date(2026, 2, 20),
    )
    session.add(
        TradeEvent(trade_id=trade.id, event_type="ASSIGN", event_date=date(2026, 2, 20))
    )
    session.commit()

    result = backfill(session)
    session.commit()

    row = session.scalar(select(CostBasis).where(CostBasis.trade_id == trade.id))
    assert row.shares == -100
    assert row.total_amount == Decimal("-6000.00")


def test_expired_option_creates_no_row(seeded_session):
    session = seeded_session
    trade = _base_trade(
        session,
        trade_type_id=_trade_type_id(session, "ROCT PUT"),
        trade_type="ROCT PUT",
        num_of_contracts=1,
        strike_price=Decimal("30.00"),
        expiration_date=date(2026, 1, 9),
    )
    session.add(TradeEvent(trade_id=trade.id, event_type="EXPIRE", event_date=date(2026, 1, 9)))
    session.commit()

    result = backfill(session)
    session.commit()

    assert result.rows_created == 0
    assert result.trades_skipped_no_event == 1
    assert session.scalar(select(CostBasis).where(CostBasis.trade_id == trade.id)) is None


def test_rocs_bull_put_spread_never_creates_cost_basis_row(seeded_session):
    """Spreads cannot be assigned; even an erroneous ASSIGN must not write
    a cost_basis row — the share-changing trade is the sibling PUT."""
    session = seeded_session
    trade = _base_trade(
        session,
        trade_type_id=_trade_type_id(session, "ROCS BULL PUT SPREAD"),
        trade_type="ROCS BULL PUT SPREAD",
        num_of_contracts=1,
        strike_price=Decimal("100.00"),
        long_strike=Decimal("95.00"),
        expiration_date=date(2026, 3, 20),
    )
    session.add(
        TradeEvent(trade_id=trade.id, event_type="ASSIGN", event_date=date(2026, 3, 20))
    )
    session.commit()

    result = backfill(session)
    session.commit()

    assert result.rows_created == 0
    assert result.flagged_spread_assignment == [trade.id]
    assert session.scalar(select(CostBasis).where(CostBasis.trade_id == trade.id)) is None


def test_closed_rocs_spread_with_sibling_put_is_not_flagged(seeded_session):
    session = seeded_session
    account_id, ticker_id = _account_ticker_ids(session)
    spread = _base_trade(
        session,
        trade_type_id=_trade_type_id(session, "ROCS BULL PUT SPREAD"),
        trade_type="ROCS BULL PUT SPREAD",
        num_of_contracts=2,
        strike_price=Decimal("200.00"),
        long_strike=Decimal("195.00"),
        expiration_date=date(2026, 6, 26),
        date_trade_open=date(2026, 5, 8),
    )
    session.add(TradeEvent(trade_id=spread.id, event_type="CLOSE", event_date=date(2026, 6, 12)))
    put = Trade(
        account_id=account_id,
        ticker_id=ticker_id,
        ticker="ADBE",
        trade_type_id=_trade_type_id(session, "RULE ONE PUT"),
        trade_type="RULE ONE PUT",
        date_trade_open=date(2026, 6, 12),
        expiration_date=date(2026, 6, 26),
        strike_price=Decimal("200.00"),
        num_of_contracts=2,
        credit_debit=Decimal("0"),
        commission_per_share=Decimal("0"),
    )
    session.add(put)
    session.commit()

    result = backfill(session)
    session.commit()

    assert result.flagged_closed_spread_missing_put == []
    assert result.rows_created == 0
    assert session.scalar(select(CostBasis).where(CostBasis.trade_id == spread.id)) is None


def test_closed_rocs_spread_without_sibling_put_is_flagged(seeded_session):
    session = seeded_session
    spread = _base_trade(
        session,
        trade_type_id=_trade_type_id(session, "ROCS BULL PUT SPREAD"),
        trade_type="ROCS BULL PUT SPREAD",
        num_of_contracts=2,
        strike_price=Decimal("200.00"),
        long_strike=Decimal("195.00"),
        expiration_date=date(2026, 6, 26),
        date_trade_open=date(2026, 5, 8),
    )
    session.add(TradeEvent(trade_id=spread.id, event_type="CLOSE", event_date=date(2026, 6, 12)))
    session.commit()

    result = backfill(session)
    session.commit()

    assert result.flagged_closed_spread_missing_put == [spread.id]
    assert result.rows_created == 0


def test_rut_bull_put_spread_assignment_is_flagged_not_backfilled(seeded_session):
    session = seeded_session
    trade = _base_trade(
        session,
        trade_type_id=_trade_type_id(session, "BULL PUT SPREAD"),
        trade_type="BULL PUT SPREAD",
        ticker="RUT",
        num_of_contracts=1,
        strike_price=Decimal("2200.00"),
        long_strike=Decimal("2190.00"),
        expiration_date=date(2026, 4, 17),
    )
    session.add(
        TradeEvent(trade_id=trade.id, event_type="ASSIGN", event_date=date(2026, 4, 17))
    )
    session.commit()

    result = backfill(session)
    session.commit()

    assert result.rows_created == 0
    assert result.flagged_spread_assignment == [trade.id]
    assert session.scalar(select(CostBasis).where(CostBasis.trade_id == trade.id)) is None


def test_backfill_is_idempotent(seeded_session):
    session = seeded_session
    trade = _base_trade(
        session,
        trade_type_id=_trade_type_id(session, "BTO"),
        trade_type="BTO",
        num_of_shares=10,
        price_per_share=Decimal("20.00"),
    )
    session.commit()

    first = backfill(session)
    session.commit()
    second = backfill(session)
    session.commit()

    assert first.rows_created == 1
    assert second.rows_created == 0
    assert second.trades_skipped_existing == 1
    rows = session.scalars(select(CostBasis).where(CostBasis.trade_id == trade.id)).all()
    assert len(rows) == 1


def test_dry_run_rolls_back(seeded_session):
    session = seeded_session
    _base_trade(
        session,
        trade_type_id=_trade_type_id(session, "BTO"),
        trade_type="BTO",
        num_of_shares=10,
        price_per_share=Decimal("20.00"),
    )
    session.commit()

    result = backfill(session, dry_run=True)

    assert result.rows_created == 1
    assert session.scalar(select(CostBasis)) is None
