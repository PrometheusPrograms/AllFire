"""Tests for scripts/backfill_okw_column_fields.py."""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models import Account, Base, Ticker, Trade, TradeType
from scripts.backfill_okw_column_fields import apply_okw_column_fields
from scripts.okw_parser import ParsedTrade


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
        seed.add(Ticker(ticker="GPN"))
        seed.commit()
    return factory


def _parsed(**overrides) -> ParsedTrade:
    defaults = dict(
        sheet_name="TRADES 2026",
        column_letter="E",
        ticker="GPN",
        trade_type_name="ROCT PUT",
        trade_date=date(2026, 3, 1),
        price_per_share=Decimal("100"),
        days_to_expiration=7,
        expiration_date=date(2026, 3, 8),
        short_strike=Decimal("90.00"),
        long_strike=None,
        credit_debit=Decimal("0.50"),
        commission_per_share=Decimal("0.0041"),
        net_credit_per_share=Decimal("0.50"),
        risk_capital_per_share=Decimal("89.50"),
        margin_percent=Decimal("1"),
        margin_capital=None,
        arorc=Decimal("0.2"),
        delta=Decimal("0.15"),
        probability_of_winning=Decimal("0.85"),
        num_of_contracts=1,
        num_of_shares=100,
        result_raw="EXPIRED",
        result_date=date(2026, 3, 8),
        closing_debit=None,
        total_debit=Decimal("0"),
        result_net_credit=None,
        final_arorc=None,
        notes=None,
    )
    defaults.update(overrides)
    return ParsedTrade(**defaults)


def test_fills_null_delta_and_probability(session_factory):
    with session_factory() as session:
        account = session.query(Account).one()
        ticker = session.query(Ticker).one()
        trade_type = session.query(TradeType).one()
        trade = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker="GPN",
            trade_type_id=trade_type.id,
            trade_type="ROCT PUT",
            date_trade_open=date(2026, 3, 1),
            expiration_date=date(2026, 3, 8),
            strike_price=Decimal("90.00"),
            num_of_contracts=1,
            credit_debit=Decimal("0.50"),
            commission_per_share=Decimal("0.0041"),
        )
        session.add(trade)
        session.flush()
        changed = apply_okw_column_fields(
            session, [_parsed()], account_name="Rule 1"
        )
        session.commit()
        session.refresh(trade)
        assert changed == [(trade.id, Decimal("0.15"), Decimal("0.85"))]
        assert trade.delta == Decimal("0.15")
        assert trade.probability_of_winning == Decimal("0.85")


def test_does_not_overwrite_existing_delta(session_factory):
    with session_factory() as session:
        account = session.query(Account).one()
        ticker = session.query(Ticker).one()
        trade_type = session.query(TradeType).one()
        trade = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker="GPN",
            trade_type_id=trade_type.id,
            trade_type="ROCT PUT",
            date_trade_open=date(2026, 3, 1),
            expiration_date=date(2026, 3, 8),
            strike_price=Decimal("90.00"),
            num_of_contracts=1,
            credit_debit=Decimal("0.50"),
            commission_per_share=Decimal("0.0041"),
            delta=Decimal("0.20"),
            probability_of_winning=Decimal("0.80"),
        )
        session.add(trade)
        session.flush()
        changed = apply_okw_column_fields(
            session, [_parsed()], account_name="Rule 1"
        )
        assert changed == []
        assert trade.delta == Decimal("0.20")


def test_fills_final_arorc_and_result_net_credit(session_factory):
    with session_factory() as session:
        account = session.query(Account).one()
        ticker = session.query(Ticker).one()
        trade_type = session.query(TradeType).one()
        trade = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker="GPN",
            trade_type_id=trade_type.id,
            trade_type="ROCT PUT",
            date_trade_open=date(2026, 3, 1),
            expiration_date=date(2026, 3, 8),
            strike_price=Decimal("90.00"),
            num_of_contracts=1,
            credit_debit=Decimal("0.50"),
            commission_per_share=Decimal("0.0041"),
        )
        session.add(trade)
        session.flush()
        apply_okw_column_fields(
            session,
            [
                _parsed(
                    result_net_credit=Decimal("20.59"),
                    final_arorc=Decimal("0.233"),
                )
            ],
            account_name="Rule 1",
        )
        session.commit()
        session.refresh(trade)
        assert trade.final_arorc == Decimal("0.233")
        assert trade.result_net_credit == Decimal("20.59")
        assert trade.probability_of_winning == Decimal("0.85")
