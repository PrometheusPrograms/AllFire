"""Tests for statement-truth corrections applied to OKW-imported trades."""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models import Account, Base, Ticker, Trade, TradeEvent, TradeType
from scripts.apply_statement_corrections import apply_corrections

TODAY = date(2026, 9, 24)
NKE_KEY = {
    "account": "Rule 1",
    "ticker": "NKE",
    "trade_type": "ROCT PUT",
    "date_trade_open": "2026-08-18",
    "expiration_date": "2026-08-28",
    "strike_price": "38.50",
}


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
            Account(account_name="Rule 1", account_type="INVESTMENT", start_date=date(2020, 1, 1))
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
        seed.add(Ticker(ticker="NKE"))
        seed.commit()
    return factory


def _put(session, *, contracts, credit, opened=date(2026, 8, 18), expires=date(2026, 8, 28)) -> int:
    account = session.query(Account).one()
    ticker = session.query(Ticker).one()
    ttype = session.query(TradeType).one()
    credit = Decimal(credit)
    net = credit - Decimal("0.0041")
    trade = Trade(
        account_id=account.id,
        ticker_id=ticker.id,
        ticker="NKE",
        trade_type_id=ttype.id,
        trade_type="ROCT PUT",
        date_trade_open=opened,
        expiration_date=expires,
        days_to_expiration=(expires - opened).days,
        num_of_contracts=contracts,
        num_of_shares=contracts * 100,
        strike_price=Decimal("38.50"),
        credit_debit=credit,
        commission_per_share=Decimal("0.0041"),
        margin_percent=Decimal("1"),
        net_credit_per_share=net,
        risk_capital_per_share=Decimal("38.50") - net,
    )
    session.add(trade)
    session.flush()
    session.add(TradeEvent(trade_id=trade.id, event_type="OPEN", event_date=opened))
    session.commit()
    return trade.id


LIMIT_PRICE_FIX = {
    **NKE_KEY,
    "id": "nke",
    "match": {"num_of_contracts": 2, "credit_debit": "0.27"},
    "set": {"num_of_contracts": 1, "num_of_shares": 100, "credit_debit": "0.22"},
    "recompute": True,
    "events": {"EXPIRE": "2026-08-28"},
    "reason": "Schwab filled 1 @ 0.22",
}


def test_fill_correction_recomputes_fields_adds_expire_and_audits(session_factory):
    with session_factory() as session:
        good_id = _put(session, contracts=1, credit="0.22")
        bad_id = _put(session, contracts=2, credit="0.27")

    with session_factory() as session:
        result = apply_corrections(session, [LIMIT_PRICE_FIX], today=TODAY)
        session.commit()
        assert result.errors == [] and len(result.applied) == 1

        fixed = session.get(Trade, bad_id)
        assert (fixed.num_of_contracts, fixed.credit_debit) == (1, Decimal("0.22"))
        assert fixed.net_credit_per_share == Decimal("0.2159")
        assert fixed.risk_capital_per_share == Decimal("38.2841")
        assert fixed.margin_capital == Decimal("3828.41")
        assert fixed.arorc == Decimal("0.205839")  # 0.2159 / 38.2841 * 365 / 10
        assert fixed.result_net_credit == Decimal("21.59")
        assert "statement truth 2026-09-24" in fixed.notes
        events = {
            e.event_type: e.event_date for e in session.query(TradeEvent).filter_by(trade_id=bad_id)
        }
        assert events["EXPIRE"] == date(2026, 8, 28)
        # The correct sibling fill is untouched.
        assert session.get(Trade, good_id).notes is None


def test_rerun_is_a_no_op(session_factory):
    with session_factory() as session:
        _put(session, contracts=1, credit="0.22")
        _put(session, contracts=2, credit="0.27")
    with session_factory() as session:
        apply_corrections(session, [LIMIT_PRICE_FIX], today=TODAY)
        session.commit()
    expire_both = {
        **NKE_KEY,
        "id": "both",
        "expect": 2,
        "events": {"EXPIRE": "2026-08-28"},
        "reason": "r",
    }
    with session_factory() as session:
        apply_corrections(session, [expire_both], today=TODAY)
        session.commit()
    with session_factory() as session:
        result = apply_corrections(session, [LIMIT_PRICE_FIX, expire_both], today=TODAY)
        assert result.already_applied == ["nke", "both"] and result.applied == []


def test_unexpected_match_count_is_an_error_and_changes_nothing(session_factory):
    with session_factory() as session:
        _put(session, contracts=1, credit="0.22")
        _put(session, contracts=1, credit="0.22")
    with session_factory() as session:
        result = apply_corrections(
            session,
            [{**NKE_KEY, "id": "x", "set": {"credit_debit": "0.20"}, "reason": "r"}],
            today=TODAY,
        )
        assert result.errors and "expected 1" in result.errors[0]


def test_saturday_expiration_moves_to_friday_with_its_expire_event(session_factory):
    with session_factory() as session:
        trade_id = _put(
            session, contracts=1, credit="0.10", opened=date(2026, 8, 7), expires=date(2026, 8, 8)
        )
        session.add(TradeEvent(trade_id=trade_id, event_type="EXPIRE", event_date=date(2026, 8, 8)))
        session.commit()
    with session_factory() as session:
        fix = {
            **NKE_KEY,
            "id": "0dte",
            "date_trade_open": "2026-08-07",
            "expiration_date": "2026-08-08",
            "set": {"expiration_date": "2026-08-07"},
            "events": {"EXPIRE": "2026-08-07"},
            "reason": "0-DTE",
        }
        apply_corrections(session, [fix], today=TODAY)
        session.commit()
        trade = session.get(Trade, trade_id)
        assert trade.expiration_date == date(2026, 8, 7)
        expire = session.query(TradeEvent).filter_by(trade_id=trade_id, event_type="EXPIRE").one()
        assert expire.event_date == date(2026, 8, 7)


def _assigned_put(session, *, contracts, assigned_on=date(2026, 8, 28)) -> int:
    """A put with an ASSIGN event and the share lot backfill_cost_basis would write."""
    from scripts.backfill_cost_basis import assignment_lot

    trade_id = _put(session, contracts=contracts, credit="0.22")
    trade = session.get(Trade, trade_id)
    assign = TradeEvent(trade_id=trade_id, event_type="ASSIGN", event_date=assigned_on)
    session.add(assign)
    session.flush()
    session.add(assignment_lot(trade, "ROCT PUT", assign))
    session.commit()
    return trade_id


def _lots(session, trade_id):
    from app.models import CostBasis

    return [
        (lot.shares, lot.cost_per_share, lot.transaction_date)
        for lot in session.query(CostBasis).filter_by(trade_id=trade_id)
    ]


def test_partial_assignment_split_keeps_assigned_and_adds_expired_sibling(session_factory):
    with session_factory() as session:
        trade_id = _assigned_put(session, contracts=2)
    split = {
        **NKE_KEY,
        "id": "split",
        "match": {"num_of_contracts": 2},
        "reason": "1 assigned, 1 expired",
        "split": {"assigned_contracts": 1, "expired_contracts": 1, "expire_date": "2026-08-28"},
    }
    with session_factory() as session:
        result = apply_corrections(session, [split], today=TODAY)
        session.commit()
        assert result.errors == []
        assert session.get(Trade, trade_id).num_of_contracts == 1
        assert _lots(session, trade_id) == [(100, Decimal("38.50"), date(2026, 8, 28))]
        sibling = session.query(Trade).filter(Trade.id != trade_id).one()
        assert sibling.num_of_contracts == 1
        assert {e.event_type for e in session.query(TradeEvent).filter_by(trade_id=sibling.id)} == {
            "OPEN",
            "EXPIRE",
        }
        assert _lots(session, sibling.id) == []
    with session_factory() as session:
        assert apply_corrections(session, [split], today=TODAY).already_applied == ["split"]


def test_assigned_that_expired_loses_its_lot_and_back_again(session_factory):
    with session_factory() as session:
        trade_id = _assigned_put(session, contracts=1)
    to_expire = {
        **NKE_KEY,
        "id": "x",
        "reason": "expired",
        "outcome": {"from": "ASSIGN", "to": "EXPIRE", "date": "2026-08-28"},
    }
    with session_factory() as session:
        apply_corrections(session, [to_expire], today=TODAY)
        session.commit()
        assert _lots(session, trade_id) == []
        assert [e.event_type for e in session.query(TradeEvent).filter_by(trade_id=trade_id)] == [
            "OPEN",
            "EXPIRE",
        ]
    to_assign = {
        **NKE_KEY,
        "id": "y",
        "reason": "assigned",
        "outcome": {"from": "EXPIRE", "to": "ASSIGN", "date": "2026-08-28"},
    }
    with session_factory() as session:
        apply_corrections(session, [to_assign], today=TODAY)
        session.commit()
        assert _lots(session, trade_id) == [(100, Decimal("38.50"), date(2026, 8, 28))]


def test_early_assignment_date_moves_the_lot_date(session_factory):
    with session_factory() as session:
        trade_id = _assigned_put(session, contracts=1)
    early = {
        **NKE_KEY,
        "id": "early",
        "reason": "early assignment",
        "events": {"ASSIGN": "2026-08-25"},
    }
    with session_factory() as session:
        apply_corrections(session, [early], today=TODAY)
        session.commit()
        assert _lots(session, trade_id) == [(100, Decimal("38.50"), date(2026, 8, 25))]


def test_remove_backs_up_and_unlinks_roll_children(session_factory):
    with session_factory() as session:
        trade_id = _assigned_put(session, contracts=1)
        child_id = _put(session, contracts=1, credit="0.30", opened=date(2026, 8, 20))
        session.get(Trade, child_id).trade_parent_id = trade_id
        session.commit()
    remove = {
        **NKE_KEY,
        "id": "rm",
        "match": {"credit_debit": "0.22"},
        "remove": True,
        "reason": "never filled",
    }
    with session_factory() as session:
        result = apply_corrections(session, [remove], today=TODAY)
        session.commit()
        assert session.get(Trade, trade_id) is None
        assert _lots(session, trade_id) == []
        assert session.get(Trade, child_id).trade_parent_id is None
        backup = result.removed[0]
        assert backup["trade"]["id"] == trade_id
        assert len(backup["cost_basis"]) == 1 and backup["unlinked_children"] == [child_id]
    with session_factory() as session:
        assert apply_corrections(session, [remove], today=TODAY).already_applied == ["rm"]


def test_add_creates_a_missing_trade_once(session_factory):
    correction = {
        **NKE_KEY,
        "id": "nke-missing",
        "date_trade_open": "2026-08-25",
        "expiration_date": "2026-09-04",
        "strike_price": "37.50",
        "add": {"num_of_contracts": 1, "credit_debit": "0.21", "commission_per_share": "0.0041"},
        "events": {"EXPIRE": "2026-09-04"},
        "reason": "Schwab API: sold 1 @ 0.21; not in OKW",
    }
    with session_factory() as session:
        result = apply_corrections(session, [correction], today=TODAY)
        session.commit()
    assert result.errors == [] and len(result.applied) == 1
    with session_factory() as session:
        trade = session.query(Trade).one()
        assert (trade.num_of_contracts, trade.num_of_shares) == (1, 100)
        assert trade.net_credit_per_share == Decimal("0.2059")
        assert trade.risk_capital_per_share == Decimal("37.2941")
        assert trade.margin_capital == Decimal("3729.41")
        assert trade.days_to_expiration == 10
        assert trade.result_net_credit == Decimal("20.59")
        assert trade.import_batch == "stmt_corrections"
        assert "not in OKW" in trade.notes
        events = sorted((e.event_type, e.event_date) for e in session.query(TradeEvent))
        assert events == [("EXPIRE", date(2026, 9, 4)), ("OPEN", date(2026, 8, 25))]
        rerun = apply_corrections(session, [correction], today=TODAY)
        assert rerun.applied == [] and rerun.already_applied == ["nke-missing"]
        assert session.query(Trade).count() == 1


def test_add_with_unknown_trade_type_is_an_error(session_factory):
    correction = {
        **NKE_KEY,
        "trade_type": "ROCT CALL",
        "add": {"num_of_contracts": 1, "credit_debit": "0.21"},
        "reason": "x",
    }
    with session_factory() as session:
        result = apply_corrections(session, [correction], today=TODAY)
    assert result.errors and "unknown trade type" in result.errors[0]


def test_recompute_uses_spread_width_for_risk_capital(session_factory):
    from scripts.apply_statement_corrections import recompute_fill_fields

    spread = Trade(
        strike_price=Decimal("130.00"),
        long_strike=Decimal("125.00"),
        credit_debit=Decimal("0.42"),
        commission_per_share=Decimal("0.00825"),
        num_of_contracts=1,
        date_trade_open=date(2026, 9, 9),
        expiration_date=date(2026, 12, 18),
    )
    recompute_fill_fields(spread)
    assert spread.risk_capital_per_share == Decimal("4.58825")
    assert spread.margin_capital == Decimal("458.83")

    call = Trade(
        strike_price=Decimal("64.00"),
        credit_debit=Decimal("0.38"),
        commission_per_share=Decimal("0.0041"),
        num_of_contracts=1,
        date_trade_open=date(2026, 9, 1),
        expiration_date=date(2026, 9, 11),
    )
    recompute_fill_fields(call)
    assert call.margin_capital is None
