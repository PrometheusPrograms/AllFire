"""Tests for scripts/schwab_loader.py against in-memory SQLite."""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.models import Account, Base, CashFlow, CostBasis, Ticker, Trade, TradeEvent, TradeType
from scripts.schwab_loader import load_schwab
from scripts.schwab_parser import CashDividend, EquityBuy


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
                type_name="ROCT PUT",
                category="OPTIONS",
                is_credit=True,
                requires_expiration=True,
                requires_strike=True,
                requires_contracts=True,
            )
        )
        seed.commit()
    return factory


def _dividend(**overrides) -> CashDividend:
    values = dict(
        activity_id="1001",
        transaction_date=date(2025, 3, 14),
        amount=Decimal("12.50"),
        ticker="AAPL",
        description="Qualified Dividend",
    )
    values.update(overrides)
    return CashDividend(**values)


def _buy(**overrides) -> EquityBuy:
    values = dict(
        activity_id="2001",
        order_id="88001",
        transaction_date=date(2025, 4, 2),
        ticker="AAPL",
        shares=10,
        price=Decimal("150"),
        fees=Decimal("0.65"),
        description="Buy AAPL",
    )
    values.update(overrides)
    return EquityBuy(**values)


def test_inserts_dividend_and_bto_with_cost_basis(session_factory):
    with session_factory() as session:
        result = load_schwab(
            session,
            account_name="Rule 1",
            import_batch="schwab_rule1_div_bto",
            dividends=[_dividend()],
            equity_buys=[_buy()],
        )
        session.commit()

    assert result.dividends_created == 1
    assert result.btos_created == 1

    with session_factory() as session:
        flow = session.scalar(select(CashFlow))
        assert flow is not None
        assert flow.transaction_type == "DIVIDEND"
        assert flow.amount == Decimal("12.50")
        assert flow.schwab_activity_id == "1001"

        trade = session.scalar(select(Trade).where(Trade.trade_type == "BTO"))
        assert trade is not None
        assert trade.num_of_shares == 10
        assert trade.price_per_share == Decimal("150")
        assert trade.schwab_activity_id == "2001"
        event = session.scalar(select(TradeEvent).where(TradeEvent.trade_id == trade.id))
        assert event is not None
        assert event.event_type == "OPEN"
        basis = session.scalar(select(CostBasis).where(CostBasis.trade_id == trade.id))
        assert basis is not None
        assert basis.shares == 10
        assert basis.total_amount == Decimal("1500.65")


def test_rerun_is_idempotent_on_activity_id(session_factory):
    with session_factory() as session:
        load_schwab(
            session,
            account_name="Rule 1",
            import_batch="schwab_rule1_div_bto",
            dividends=[_dividend()],
            equity_buys=[_buy()],
        )
        session.commit()

    with session_factory() as session:
        result = load_schwab(
            session,
            account_name="Rule 1",
            import_batch="schwab_rule1_div_bto",
            dividends=[_dividend()],
            equity_buys=[_buy()],
        )
        session.commit()

    assert result.dividends_created == 0
    assert result.dividends_skipped == 1
    assert result.btos_created == 0
    assert result.btos_skipped_existing == 1
    with session_factory() as session:
        assert len(session.scalars(select(CashFlow)).all()) == 1


def test_skips_buy_that_matches_assign_lot(session_factory):
    with session_factory() as session:
        account = session.scalar(select(Account))
        put_type = session.scalar(select(TradeType).where(TradeType.type_name == "ROCT PUT"))
        ticker = Ticker(ticker="AAPL")
        session.add(ticker)
        session.flush()
        trade = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker="AAPL",
            trade_type_id=put_type.id,
            trade_type="ROCT PUT",
            date_trade_open=date(2025, 3, 1),
            expiration_date=date(2025, 4, 18),
            num_of_contracts=1,
            strike_price=Decimal("150"),
            credit_debit=Decimal("1.00"),
            commission_per_share=Decimal("0"),
        )
        session.add(trade)
        session.flush()
        session.add(
            TradeEvent(
                trade_id=trade.id,
                event_type="ASSIGN",
                event_date=date(2025, 4, 18),
            )
        )
        session.add(
            CostBasis(
                account_id=account.id,
                ticker_id=ticker.id,
                trade_id=trade.id,
                transaction_date=date(2025, 4, 18),
                shares=100,
                cost_per_share=Decimal("150"),
                total_amount=Decimal("15000"),
            )
        )
        session.commit()

    with session_factory() as session:
        result = load_schwab(
            session,
            account_name="Rule 1",
            import_batch="schwab_rule1_div_bto",
            dividends=[],
            equity_buys=[
                _buy(
                    activity_id="2004",
                    order_id=None,
                    transaction_date=date(2025, 4, 18),
                    shares=100,
                    price=Decimal("150"),
                    fees=Decimal("0"),
                )
            ],
        )
        session.commit()

    assert result.btos_created == 0
    assert result.btos_skipped_assign == 1


def test_skips_buy_matching_existing_bto_without_schwab_id(session_factory):
    with session_factory() as session:
        account = session.scalar(select(Account))
        bto_type = session.scalar(select(TradeType).where(TradeType.type_name == "BTO"))
        ticker = Ticker(ticker="AAPL")
        session.add(ticker)
        session.flush()
        session.add(
            Trade(
                account_id=account.id,
                ticker_id=ticker.id,
                ticker="AAPL",
                trade_type_id=bto_type.id,
                trade_type="BTO",
                date_trade_open=date(2025, 4, 2),
                num_of_shares=10,
                price_per_share=Decimal("150.00"),
                credit_debit=Decimal("150.00"),
                commission_per_share=Decimal("0"),
            )
        )
        session.commit()

    with session_factory() as session:
        result = load_schwab(
            session,
            account_name="Rule 1",
            import_batch="schwab_rule1_div_bto",
            dividends=[],
            equity_buys=[_buy()],
        )
        session.commit()

    assert result.btos_created == 0
    assert result.btos_skipped_existing == 1


def test_dry_run_does_not_write(session_factory):
    with session_factory() as session:
        result = load_schwab(
            session,
            account_name="Rule 1",
            import_batch="schwab_rule1_div_bto",
            dividends=[_dividend()],
            equity_buys=[_buy()],
            dry_run=True,
        )
        session.commit()

    assert result.dividends_created == 1
    assert result.btos_created == 1
    with session_factory() as session:
        assert session.scalars(select(CashFlow)).first() is None
        assert session.scalars(select(Trade)).first() is None
