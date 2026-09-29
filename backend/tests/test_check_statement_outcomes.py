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


from scripts.check_statement_outcomes import unresolved  # noqa: E402

TODAY = date(2026, 9, 27)


def _open_trade(id_, contracts=1, strike="38.00", trade_type="ROCT PUT", long_strike=None,
                expiration=EXP):
    trade = _trade(id_, contracts, strike, trade_type)
    trade.long_strike = long_strike
    trade.expiration_date = expiration
    return trade


def test_open_trade_past_expiration_is_not_mislabelled_by_compare():
    ledger = {_key(): StatementContract(sold=Decimal(1), assigned=Decimal(1), assigned_on=EXP)}
    assert compare(ledger, [(_open_trade(963), "Roth", _events())], COVERAGE_END) == []


def test_unresolved_early_assignment_proposes_assign_on_statement_date():
    early = date(2026, 3, 3)
    ledger = {_key(): StatementContract(sold=Decimal(1), assigned=Decimal(1), assigned_on=early)}
    [finding] = unresolved(ledger, [(_open_trade(963), "Roth", _events())], COVERAGE_END, TODAY)
    assert (finding.kind, finding.proposal) == ("unresolved", {"events": {"ASSIGN": "2026-03-03"}})


def test_unresolved_spread_expired_by_its_short_leg():
    ledger = {
        _key("60.00"): StatementContract(sold=Decimal(4), expired=Decimal(4)),
        _key("55.00"): StatementContract(bought=Decimal(4), expired=Decimal(4)),
    }
    spread = _open_trade(480, 4, "60.00", "ROCS BULL PUT SPREAD", long_strike=Decimal("55.00"))
    [finding] = unresolved(ledger, [(spread, "Roth", _events())], COVERAGE_END, TODAY)
    assert finding.proposal == {"events": {"EXPIRE": "2026-03-13"}}


def test_buyback_with_same_day_sale_is_a_roll_dated_the_trade_day():
    settled = date(2026, 3, 9)  # Monday settlement -> traded Friday 03-06
    ledger = {
        _key(): StatementContract(sold=Decimal(1), bought=Decimal(1), bought_on=[settled]),
        ("Roth", "ONON", date(2026, 3, 20), Decimal("37.00"), "PUT"): StatementContract(
            sold=Decimal(1), sold_on=[settled]
        ),
    }
    [finding] = unresolved(ledger, [(_open_trade(461), "Roth", _events())], COVERAGE_END, TODAY)
    assert finding.proposal == {"events": {"ROLL": "2026-03-06"}}
    del ledger[("Roth", "ONON", date(2026, 3, 20), Decimal("37.00"), "PUT")]
    [finding] = unresolved(ledger, [(_open_trade(461), "Roth", _events())], COVERAGE_END, TODAY)
    assert finding.proposal == {"events": {"CLOSE": "2026-03-06"}}


def test_unsettled_or_post_statement_trades_are_listed_not_guessed():
    ledger = {_key(): StatementContract(sold=Decimal(2), assigned=Decimal(1), expired=Decimal(1))}
    [ask] = unresolved(ledger, [(_open_trade(1, 2), "Roth", _events())], COVERAGE_END, TODAY)
    assert (ask.kind, ask.proposal) == ("ask", None)
    late = _open_trade(2, expiration=date(2026, 9, 18))
    [api] = unresolved({}, [(late, "Roth", _events())], COVERAGE_END, TODAY)
    assert api.kind == "needs_api"
    not_yet = _open_trade(3, expiration=date(2026, 10, 2))
    assert unresolved({}, [(not_yet, "Roth", _events())], COVERAGE_END, TODAY) == []


def test_spread_long_leg_closing_sales_are_not_short_contracts():
    # SFM 2026-02-20 $60P: long leg bought 01/09 (2), sold to close 01/22-23 (2),
    # plus a separate short sale of 2 that expired.
    stmt = StatementContract(
        sold=Decimal(4), bought=Decimal(2), expired=Decimal(2),
        sold_on=[date(2026, 1, 22), date(2026, 1, 23)], bought_on=[date(2026, 1, 9)],
        sale_prices=[Decimal("1.08"), Decimal("1.25"), Decimal("1.25")],
    )
    trade = _trade(39, 2, "60.00")
    trade.credit_debit = Decimal("1.25")
    assert compare({_key("60.00"): stmt}, [(trade, "Roth", _events("EXPIRE"))], COVERAGE_END) == []


def test_fill_price_mismatch_proposes_the_statement_price_but_not_for_rolls():
    stmt = StatementContract(
        sold=Decimal(4), assigned=Decimal(4), assigned_on=EXP,
        sold_on=[date(2026, 3, 3)], sale_prices=[Decimal("0.32")],
    )
    trade = _trade(799, 4)
    trade.credit_debit, trade.trade_parent_id = Decimal("0.20"), None
    found = _kinds(compare({_key(): stmt}, [(trade, "Roth", _events("ASSIGN"))], COVERAGE_END))
    assert found["fill_price"].proposal["set"] == {"credit_debit": "0.32"}
    trade.trade_parent_id = 798
    assert compare({_key(): stmt}, [(trade, "Roth", _events("ASSIGN"))], COVERAGE_END) == []


def test_roll_and_conversion_legs_are_continuations():
    from scripts.check_statement_outcomes import continuation_ids

    def leg(id_, opened, trade_type="ROCT PUT", long_strike=None):
        t = _trade(id_, 1, trade_type=trade_type)
        t.date_trade_open, t.long_strike, t.trade_parent_id = opened, long_strike, None
        return t

    rolled = (leg(1, date(2026, 3, 2)), "Roth",
              [SimpleNamespace(event_type="ROLL", event_date=date(2026, 3, 9))])
    spread = (leg(2, date(2026, 1, 8), "ROCS BULL PUT SPREAD", Decimal("55")), "Roth",
              [SimpleNamespace(event_type="CLOSE", event_date=date(2026, 1, 22))])
    new_leg = (leg(3, date(2026, 3, 9)), "Roth", _events())
    converted = (leg(4, date(2026, 1, 22)), "Roth", _events())
    unrelated = (leg(5, date(2026, 4, 1)), "Roth", _events())
    assert continuation_ids([rolled, spread, new_leg, converted, unrelated]) == {3, 4}

