"""Tests for the read-only Schwab-order roll checker (synthetic API payloads)."""

from datetime import date
from decimal import Decimal

from scripts.check_rolls import check, find_rolls

OLD = "TSLA  250117C00360000"
NEW = "TSLA  250124C00450000"


def _leg(symbol, amount, price, effect):
    return {
        "instrument": {"assetType": "OPTION", "symbol": symbol},
        "amount": amount,
        "price": price,
        "positionEffect": effect,
    }


def _txn(activity_id, order_id, *legs, day="2025-01-07T19:19:35+0000"):
    return {"activityId": activity_id, "orderId": order_id, "type": "TRADE", "tradeDate": day,
            "transferItems": list(legs)}


def _roll_order():
    return [
        _txn(1, 900, _leg(NEW, -2, 4.77, "OPENING"), _leg(OLD, 2, 38.27, "CLOSING")),
        _txn(2, 900, _leg(NEW, -2, 4.78, "OPENING"), _leg(OLD, 2, 38.26, "CLOSING")),
        _txn(3, 901, _leg("TSLA  250124C00460000", -1, 3.0, "OPENING")),  # plain sale
    ]


def _trade(id_, strike, expiration, opened, credit, parent=None, events=()):
    return {
        "id": id_, "ticker": "TSLA", "trade_type": "ROCT CALL", "date_trade_open": opened,
        "expiration_date": expiration, "strike_price": Decimal(strike), "credit_debit": Decimal(credit),
        "trade_parent_id": parent, "events": list(events),
    }


def test_one_order_closing_and_opening_is_a_roll_with_its_net():
    [roll] = find_rolls(_roll_order())
    assert roll.order_id == "900" and roll.on == date(2025, 1, 7)
    [opened_key] = roll.opened
    assert roll.net(opened_key) == Decimal("-33.4900")


def test_consistent_roll_has_no_findings():
    parent = _trade(875, "360", date(2025, 1, 17), date(2024, 11, 22), "-34.93",
                    events=[("OPEN", date(2024, 11, 22)), ("ROLL", date(2025, 1, 7))])
    child = _trade(876, "450", date(2025, 1, 24), date(2025, 1, 7), "-33.49", parent=875)
    assert check(find_rolls(_roll_order()), [parent, child], "Roth") == []


def test_misdated_roll_unlinked_leg_and_wrong_net_are_reported():
    parent = _trade(875, "360", date(2025, 1, 17), date(2024, 11, 22), "-34.93",
                    events=[("OPEN", date(2024, 11, 22)), ("ROLL", date(2024, 11, 22))])
    child = _trade(876, "450", date(2025, 1, 24), date(2025, 1, 6), "4.77")
    findings = {f.kind: f for f in check(find_rolls(_roll_order()), [parent, child], "Roth")}
    assert findings["roll_date"].proposal["events"] == {"ROLL": "2025-01-07"}
    fix = findings["open_date"].proposal
    assert fix["set"] == {"date_trade_open": "2025-01-07"}
    assert fix["parent"]["strike_price"] == "360.00"
    assert findings["net_credit"].proposal["set"] == {"credit_debit": "-33.49"}
    assert findings["net_credit"].proposal["match"] == {"credit_debit": "4.7700"}


def test_missing_leg_is_reported_not_guessed():
    findings = check(find_rolls(_roll_order()), [], "Roth")
    assert {f.kind for f in findings} == {"missing_leg"}


def test_opening_a_spread_is_not_a_roll():
    spread = [_txn(5, 950, _leg("TSLA  250124P00400000", -1, 5.0, "OPENING"),
                   _leg("TSLA  250124P00390000", 1, 3.0, "OPENING"))]
    assert find_rolls(spread) == []


def test_rolled_leg_is_preferred_over_a_same_key_assigned_leg():
    rolled = _trade(363, "360", date(2025, 1, 17), date(2024, 11, 22), "0.13",
                    events=[("OPEN", date(2024, 11, 22)), ("ROLL", date(2025, 1, 7))])
    assigned = _trade(362, "360", date(2025, 1, 17), date(2024, 11, 22), "0.13",
                      events=[("OPEN", date(2024, 11, 22)), ("ASSIGN", date(2025, 1, 7))])
    child = _trade(876, "450", date(2025, 1, 24), date(2025, 1, 7), "-33.49", parent=363)
    assert check(find_rolls(_roll_order()), [assigned, rolled, child], "Roth") == []



def test_roll_booked_as_a_parent_closing_debit_keeps_okw_credits():
    parent = _trade(875, "360", date(2025, 1, 17), date(2024, 11, 22), "-34.93",
                    events=[("OPEN", date(2024, 11, 22), None), ("ROLL", date(2025, 1, 7), Decimal("0.35"))])
    child = _trade(876, "450", date(2025, 1, 24), date(2025, 1, 7), "1.78", parent=875)
    assert check(find_rolls(_roll_order()), [parent, child], "Roth") == []
