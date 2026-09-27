"""Tests for the read-only statement outcome checker (synthetic ledger/trades)."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from scripts.check_statement_outcomes import StatementContract, compare

EXP = date(2026, 3, 13)
COVERAGE_END = date(2026, 8, 31)


def _trade(id_, contracts, strike="38.00", trade_type="ROCT PUT"):
    return SimpleNamespace(
        id=id_,
        ticker="ONON",
        trade_type=trade_type,
        expiration_date=EXP,
        strike_price=Decimal(strike),
        num_of_contracts=contracts,
    )


def _events(*types):
    return [SimpleNamespace(event_type=t, event_date=EXP) for t in ("OPEN", *types)]


def _key(strike="38.00", right="PUT"):
    return ("Roth", "ONON", EXP, Decimal(strike), right)


def _kinds(findings):
    return {f.kind: f for f in findings}


def test_partial_assignment_proposes_split():
    ledger = {_key(): StatementContract(sold=Decimal(2), assigned=Decimal(1), expired=Decimal(1))}
    found = _kinds(compare(ledger, [(_trade(632, 2), "Roth", _events("ASSIGN"))], COVERAGE_END))
    split = found["partial_assignment"].proposal["split"]
    assert split == {"assigned_contracts": 1, "expired_contracts": 1, "expire_date": "2026-03-13"}


def test_assigned_in_db_but_expired_on_statement():
    ledger = {_key(): StatementContract(sold=Decimal(1), expired=Decimal(1))}
    found = _kinds(compare(ledger, [(_trade(1, 1), "Roth", _events("ASSIGN"))], COVERAGE_END))
    assert found["assigned_but_expired"].proposal["outcome"] == {
        "from": "ASSIGN",
        "to": "EXPIRE",
        "date": "2026-03-13",
    }


def test_expired_in_db_but_assigned_on_statement():
    ledger = {_key(): StatementContract(sold=Decimal(3), assigned=Decimal(3), assigned_on=EXP)}
    found = _kinds(compare(ledger, [(_trade(864, 3), "Roth", _events("EXPIRE"))], COVERAGE_END))
    assert found["expired_but_assigned"].proposal["outcome"]["to"] == "ASSIGN"


def test_strike_typo_points_at_the_unmatched_statement_contract():
    ledger = {_key("38.50"): StatementContract(sold=Decimal(1), expired=Decimal(1))}
    found = _kinds(
        compare(ledger, [(_trade(377, 1, "38.00"), "Roth", _events("EXPIRE"))], COVERAGE_END)
    )
    assert found["strike_typo"].proposal == {"set": {"strike_price": "38.50"}}


def test_matching_trade_produces_no_findings():
    ledger = {_key(): StatementContract(sold=Decimal(2), expired=Decimal(2))}
    trades = [(_trade(1, 1), "Roth", _events("EXPIRE")), (_trade(2, 1), "Roth", _events("EXPIRE"))]
    assert compare(ledger, trades, COVERAGE_END) == []


def test_contracts_expiring_after_the_newest_statement_are_skipped():
    assert compare({}, [(_trade(1, 1), "Roth", _events())], date(2026, 3, 1)) == []
