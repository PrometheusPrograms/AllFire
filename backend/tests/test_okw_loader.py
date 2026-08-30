"""Tests for scripts/okw_loader.py — DB-touching behavior (get-or-create,
idempotency, unknown-type handling) — against a throwaway in-memory SQLite
DB, not the real app database. Uses `ParsedTrade` fixtures built directly
rather than a real workbook, since that's `okw_parser.py`'s job to test.
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models import Account, Base, TradeType
from scripts.okw_loader import ImportBatchExistsError, load_parsed_trades
from scripts.okw_parser import ParsedTrade


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _register_now(dbapi_connection: sqlite3.Connection, _):
        dbapi_connection.create_function("now", 0, lambda: "2026-01-01 00:00:00")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    with factory() as seed_session:
        seed_session.add(
            Account(
                account_name="Rule 1",
                account_type="INVESTMENT",
                start_date=date(2020, 1, 1),
            )
        )
        seed_session.add(
            TradeType(
                type_name="ROCT PUT",
                category="OPTIONS",
                is_credit=True,
                requires_expiration=True,
                requires_strike=True,
                requires_contracts=True,
            )
        )
        seed_session.commit()

    return factory


def _make_trade(**overrides) -> ParsedTrade:
    defaults = dict(
        sheet_name="TRADES 2026",
        column_letter="E",
        ticker="SLV",
        trade_type_name="ROCT PUT",
        trade_date=date(2026, 1, 2),
        price_per_share=Decimal("85.04"),
        days_to_expiration=7,
        expiration_date=date(2026, 1, 9),
        short_strike=Decimal("55.5"),
        long_strike=None,
        credit_debit=Decimal("0.22"),
        commission_per_share=Decimal("0.0041"),
        net_credit_per_share=Decimal("0.2159"),
        risk_capital_per_share=Decimal("55.2841"),
        margin_percent=Decimal("1"),
        margin_capital=Decimal("11056.82"),
        arorc=Decimal("0.2036"),
        num_of_contracts=2,
        num_of_shares=200,
        result_raw="EXPIRED",
        result_date=date(2026, 1, 9),
        closing_debit=None,
        total_debit=Decimal("0"),
        notes=None,
    )
    defaults.update(overrides)
    return ParsedTrade(**defaults)


def test_loads_a_trade_and_its_open_and_closing_events(session_factory):
    with session_factory() as session:
        result = load_parsed_trades(
            session, [_make_trade()], account_name="Rule 1", import_batch="test_batch"
        )
        session.commit()

    assert result.trades_created == 1
    assert result.events_created == 2  # OPEN + EXPIRE
    assert result.needs_review == 0


def test_rerunning_the_same_batch_without_force_is_refused(session_factory):
    with session_factory() as session:
        load_parsed_trades(
            session, [_make_trade()], account_name="Rule 1", import_batch="dupe"
        )
        session.commit()

    with session_factory() as session:
        with pytest.raises(ImportBatchExistsError):
            load_parsed_trades(
                session, [_make_trade()], account_name="Rule 1", import_batch="dupe"
            )


def test_force_replaces_the_existing_batch_instead_of_duplicating(session_factory):
    with session_factory() as session:
        load_parsed_trades(
            session, [_make_trade()], account_name="Rule 1", import_batch="dupe"
        )
        session.commit()

    with session_factory() as session:
        result = load_parsed_trades(
            session,
            [_make_trade(), _make_trade(ticker="GLW")],
            account_name="Rule 1",
            import_batch="dupe",
            force=True,
        )
        session.commit()

    assert result.trades_created == 2  # replaced, not appended to


def test_unknown_trade_type_is_skipped_not_guessed(session_factory):
    with session_factory() as session:
        result = load_parsed_trades(
            session,
            [_make_trade(trade_type_name="SOME NEW STRATEGY")],
            account_name="Rule 1",
            import_batch="test_batch",
        )
        session.commit()

    assert result.trades_created == 0
    assert result.unknown_trade_types == ["SOME NEW STRATEGY"]


def test_reuses_existing_ticker_row_across_trades(session_factory):
    with session_factory() as session:
        load_parsed_trades(
            session,
            [_make_trade(), _make_trade(trade_date=date(2026, 2, 1))],
            account_name="Rule 1",
            import_batch="test_batch",
        )
        session.commit()

        from sqlalchemy import select

        from app.models import Ticker

        tickers = session.scalars(select(Ticker).where(Ticker.ticker == "SLV")).all()
        assert len(tickers) == 1
