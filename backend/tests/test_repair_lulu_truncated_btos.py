"""Tests for deleting the truncated LULU 9-share Schwab BTOs."""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.models import Account, Base, CostBasis, Ticker, Trade, TradeEvent, TradeType
from scripts.repair_lulu_truncated_btos import (
    find_truncated_lulu_btos,
    repair_truncated_lulu_btos,
)


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
                is_credit=True,
                requires_shares=True,
            )
        )
        seed.add(Ticker(ticker="LULU"))
        seed.commit()
    return factory


def test_deletes_only_the_two_nine_share_lulu_btos(session_factory):
    with session_factory() as session:
        account = session.query(Account).one()
        ticker = session.query(Ticker).one()
        ttype = session.query(TradeType).filter_by(type_name="BTO").one()

        def add_bto(open_date, shares, batch="schwab_rule1_div_bto"):
            trade = Trade(
                account_id=account.id,
                ticker_id=ticker.id,
                ticker="LULU",
                trade_type_id=ttype.id,
                trade_type="BTO",
                date_trade_open=open_date,
                num_of_shares=shares,
                price_per_share=Decimal("218.99"),
                credit_debit=Decimal("218.99"),
                commission_per_share=Decimal("0"),
                import_batch=batch,
            )
            session.add(trade)
            session.flush()
            session.add(
                TradeEvent(trade_id=trade.id, event_type="OPEN", event_date=open_date)
            )
            session.add(
                CostBasis(
                    account_id=account.id,
                    ticker_id=ticker.id,
                    trade_id=trade.id,
                    transaction_date=open_date,
                    shares=shares,
                    cost_per_share=Decimal("218.99"),
                    total_amount=Decimal(shares) * Decimal("218.99"),
                )
            )
            return trade.id

        bad_a = add_bto(date(2025, 7, 24), 9)
        bad_b = add_bto(date(2025, 7, 29), 9)
        keep = add_bto(date(2025, 6, 20), 10)
        session.commit()

        found = find_truncated_lulu_btos(session)
        assert {t.id for t in found} == {bad_a, bad_b}
        deleted = repair_truncated_lulu_btos(session, dry_run=False)
        session.commit()
        assert set(deleted) == {bad_a, bad_b}
        remaining = list(session.scalars(select(Trade)).all())
        assert len(remaining) == 3  # keep June 10-share lot + two recreated July lots
        by_date = {t.date_trade_open: t for t in remaining}
        assert by_date[date(2025, 6, 20)].id == keep
        assert by_date[date(2025, 6, 20)].num_of_shares == 10
        assert by_date[date(2025, 7, 24)].num_of_shares == 10
        assert by_date[date(2025, 7, 29)].num_of_shares == 10
        july_shares = {
            row.transaction_date: row.shares
            for row in session.scalars(select(CostBasis)).all()
            if row.transaction_date in (date(2025, 7, 24), date(2025, 7, 29))
        }
        assert july_shares == {date(2025, 7, 24): 10, date(2025, 7, 29): 10}
