"""Tests for restating GOOG/GOOGL 20:1 split lots onto the original BTO."""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.models import Account, Base, CostBasis, Ticker, Trade, TradeEvent, TradeType
from scripts.repair_641_split_lots import repair_split_lots


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
        seed.add(Ticker(ticker="GOOGL"))
        seed.add(Ticker(ticker="GOOG"))
        seed.commit()
    return factory


def _add_bto(session, *, ticker: str, open_date: date, shares: int, price: Decimal) -> Trade:
    account = session.query(Account).one()
    ticker_row = session.query(Ticker).filter_by(ticker=ticker).one()
    ttype = session.query(TradeType).filter_by(type_name="BTO").one()
    trade = Trade(
        account_id=account.id,
        ticker_id=ticker_row.id,
        ticker=ticker,
        trade_type_id=ttype.id,
        trade_type="BTO",
        date_trade_open=open_date,
        num_of_shares=shares,
        price_per_share=price,
        credit_debit=price,
        commission_per_share=Decimal("0"),
        import_batch="acct641_migration",
    )
    session.add(trade)
    session.flush()
    session.add(TradeEvent(trade_id=trade.id, event_type="OPEN", event_date=open_date))
    session.add(
        CostBasis(
            account_id=account.id,
            ticker_id=ticker_row.id,
            trade_id=trade.id,
            transaction_date=open_date,
            description=f"BTO {ticker} {shares}",
            shares=shares,
            cost_per_share=price,
            total_amount=price * shares,
        )
    )
    return trade


def test_restates_googl_split_onto_original_acquisition_date(session_factory):
    with session_factory() as session:
        _add_bto(
            session,
            ticker="GOOGL",
            open_date=date(2014, 2, 25),
            shares=5,
            price=Decimal("609.2380"),
        )
        _add_bto(
            session,
            ticker="GOOGL",
            open_date=date(2022, 7, 18),
            shares=95,
            price=Decimal("0"),
        )
        session.commit()

        ids = repair_split_lots(session)
        session.commit()
        assert len(ids) == 2

        googl = session.scalars(select(Trade).where(Trade.ticker == "GOOGL")).all()
        assert len(googl) == 1
        assert googl[0].date_trade_open == date(2014, 2, 25)
        assert googl[0].num_of_shares == 100
        assert googl[0].price_per_share == Decimal("30.4619")
        basis = session.scalar(select(CostBasis).where(CostBasis.trade_id == googl[0].id))
        assert basis is not None
        assert basis.shares == 100
        assert basis.total_amount == Decimal("3046.19")
        split_lots = session.scalars(
            select(Trade).where(Trade.date_trade_open == date(2022, 7, 18))
        ).all()
        assert split_lots == []
