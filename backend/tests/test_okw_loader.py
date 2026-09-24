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

from app.models import Account, Base, TradeEvent, TradeType
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
        delta=None,
        probability_of_winning=None,
        num_of_contracts=2,
        num_of_shares=200,
        result_raw="EXPIRED",
        result_date=date(2026, 1, 9),
        closing_debit=None,
        total_debit=Decimal("0"),
        result_net_credit=None,
        final_arorc=None,
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


def test_stores_okw_column_and_result_block_fields(session_factory):
    from app.models import Trade

    with session_factory() as session:
        load_parsed_trades(
            session,
            [
                _make_trade(
                    ticker="RBLX",
                    trade_date=date(2026, 7, 22),
                    expiration_date=date(2026, 7, 31),
                    days_to_expiration=9,
                    price_per_share=Decimal("50.16"),
                    short_strike=Decimal("36.00"),
                    credit_debit=Decimal("0.21"),
                    arorc=Decimal("0.233"),
                    delta=Decimal("0.05"),
                    probability_of_winning=Decimal("0.95"),
                    num_of_contracts=1,
                    num_of_shares=100,
                    result_raw="ROLL",
                    result_date=date(2026, 7, 31),
                    closing_debit=Decimal("0"),
                    result_net_credit=Decimal("20.59"),
                    final_arorc=Decimal("0.233"),
                ),
                _make_trade(
                    column_letter="F",
                    ticker="RBLX",
                    trade_date=date(2026, 7, 31),
                    expiration_date=date(2026, 8, 7),
                    days_to_expiration=7,
                    price_per_share=Decimal("50.16"),
                    short_strike=Decimal("34.50"),
                    credit_debit=Decimal("0.02"),
                    arorc=Decimal("0.024"),
                    delta=Decimal("0.05"),
                    probability_of_winning=Decimal("0.95"),
                    num_of_contracts=1,
                    num_of_shares=100,
                    result_raw="EXPIRED",
                    result_date=None,
                    closing_debit=None,
                    result_net_credit=Decimal("22.18"),
                    final_arorc=Decimal("0.335"),
                ),
            ],
            account_name="Rule 1",
            import_batch="rblx_example",
        )
        session.commit()

        rolled, expired = session.query(Trade).order_by(Trade.date_trade_open).all()
        assert rolled.delta == Decimal("0.05")
        assert rolled.probability_of_winning == Decimal("0.95")
        assert rolled.final_arorc == Decimal("0.233")
        assert rolled.result_net_credit == Decimal("20.59")
        assert expired.trade_parent_id == rolled.id
        assert expired.final_arorc == Decimal("0.335")
        assert expired.result_net_credit == Decimal("22.18")
        expire = (
            session.query(TradeEvent)
            .filter_by(trade_id=expired.id, event_type="EXPIRE")
            .one()
        )
        assert expire.event_date == date(2026, 8, 7)
        assert expire.closing_debit == Decimal("0")


def test_closing_event_date_uses_trade_date_not_result_date(session_factory):
    """RESULT DATE is hand-typed and     unreliable — CLOSE/ROLL/ASSIGN events
    should use the trade's own date, not the (possibly stale) result date."""
    with session_factory() as session:
        load_parsed_trades(
            session,
            [
                _make_trade(
                    trade_date=date(2026, 1, 2),
                    result_raw="ASSIGNED",
                    result_date=date(2026, 3, 15),  # deliberately wrong/stale
                )
            ],
            account_name="Rule 1",
            import_batch="test_batch",
        )
        session.commit()

        event = session.query(TradeEvent).filter_by(event_type="ASSIGN").one()
        assert event.event_date == date(2026, 1, 2)


def test_expire_event_date_uses_expiration_date_not_result_date(session_factory):
    with session_factory() as session:
        load_parsed_trades(
            session,
            [
                _make_trade(
                    trade_date=date(2026, 1, 2),
                    expiration_date=date(2026, 1, 9),
                    result_raw="EXPIRED",
                    result_date=date(2026, 1, 12),  # deliberately wrong/stale
                )
            ],
            account_name="Rule 1",
            import_batch="test_batch",
        )
        session.commit()

        event = session.query(TradeEvent).filter_by(event_type="EXPIRE").one()
        assert event.event_date == date(2026, 1, 9)


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

        from app.models import Ticker, Trade

        tickers = session.scalars(select(Ticker).where(Ticker.ticker == "SLV")).all()
        assert len(tickers) == 1


def test_adjacent_roll_sets_trade_parent_id(session_factory):
    with session_factory() as session:
        load_parsed_trades(
            session,
            [
                _make_trade(
                    result_raw="ROLL",
                    result_date=date(2026, 1, 9),
                    expiration_date=date(2026, 1, 9),
                    short_strike=Decimal("55.50"),
                    num_of_contracts=1,
                    num_of_shares=100,
                ),
                _make_trade(
                    column_letter="G",
                    trade_date=date(2026, 1, 9),
                    expiration_date=date(2026, 1, 16),
                    result_raw="EXPIRED",
                    result_date=date(2026, 1, 16),
                    short_strike=Decimal("54.00"),
                    num_of_contracts=1,
                    num_of_shares=100,
                ),
            ],
            account_name="Rule 1",
            import_batch="roll_chain",
        )
        session.commit()

        from sqlalchemy import select

        from app.models import Trade, TradeEvent

        trades = session.scalars(select(Trade).order_by(Trade.id)).all()
        assert len(trades) == 2
        assert trades[0].trade_parent_id is None
        assert trades[1].trade_parent_id == trades[0].id
        roll = session.scalar(
            select(TradeEvent).where(
                TradeEvent.trade_id == trades[0].id, TradeEvent.event_type == "ROLL"
            )
        )
        assert roll.event_date == date(2026, 1, 9)


def test_pending_rolls_fifo_does_not_attach_later_original_strike(session_factory):
    """RBLX: $36 rolled 31 JUL must not parent the $36.50 opened 30 JUL."""
    with session_factory() as session:
        load_parsed_trades(
            session,
            [
                _make_trade(
                    ticker="RBLX",
                    column_letter="E",
                    trade_date=date(2026, 7, 22),
                    expiration_date=date(2026, 7, 31),
                    result_raw="ROLL",
                    result_date=date(2026, 7, 31),
                    short_strike=Decimal("36.00"),
                    num_of_contracts=1,
                    num_of_shares=100,
                ),
                _make_trade(
                    ticker="RBLX",
                    column_letter="F",
                    trade_date=date(2026, 7, 30),
                    expiration_date=date(2026, 7, 31),
                    result_raw="ROLL",
                    result_date=date(2026, 7, 31),
                    short_strike=Decimal("36.50"),
                    num_of_contracts=1,
                    num_of_shares=100,
                ),
                _make_trade(
                    ticker="RBLX",
                    column_letter="G",
                    trade_date=date(2026, 7, 31),
                    expiration_date=date(2026, 8, 7),
                    result_raw="EXPIRED",
                    result_date=date(2026, 8, 7),
                    short_strike=Decimal("34.50"),
                    num_of_contracts=1,
                    num_of_shares=100,
                ),
                _make_trade(
                    ticker="RBLX",
                    column_letter="H",
                    trade_date=date(2026, 7, 31),
                    expiration_date=date(2026, 8, 14),
                    result_raw="EXPIRED",
                    result_date=date(2026, 8, 14),
                    short_strike=Decimal("34.50"),
                    num_of_contracts=1,
                    num_of_shares=100,
                ),
            ],
            account_name="Rule 1",
            import_batch="rblx_parallel",
        )
        session.commit()

        from sqlalchemy import select

        from app.models import Trade

        trades = session.scalars(select(Trade).order_by(Trade.id)).all()
        assert [t.strike_price for t in trades] == [
            Decimal("36.00"),
            Decimal("36.50"),
            Decimal("34.50"),
            Decimal("34.50"),
        ]
        assert trades[0].trade_parent_id is None
        assert trades[1].trade_parent_id is None
        assert trades[2].trade_parent_id == trades[0].id
        assert trades[3].trade_parent_id == trades[1].id
