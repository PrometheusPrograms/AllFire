"""Tests for the read-only Schwab API vs DB/OKW recent-activity checker."""

from datetime import date
from decimal import Decimal

from scripts.check_schwab_activity import classify_legs, compare, parse_occ_symbol


def _option_txn(activity_id, day, symbol, amount, price, *, txn_type="TRADE", description="", effect=None, extra=()):
    item = {
        "instrument": {"assetType": "OPTION", "symbol": symbol},
        "amount": amount,
        "price": price,
    }
    if effect:
        item["positionEffect"] = effect
    return {
        "activityId": activity_id,
        "tradeDate": f"{day}T14:30:00+0000",
        "type": txn_type,
        "description": description,
        "transferItems": [item, *extra],
    }


def _db_trade(**overrides):
    values = dict(
        id=2000,
        ticker="ONON",
        trade_type="ROCT PUT",
        date_trade_open=date(2026, 9, 8),
        expiration_date=date(2026, 9, 18),
        strike_price=Decimal("27.50"),
        long_strike=None,
        num_of_contracts=1,
        credit_debit=Decimal("0.2500"),
        import_batch="okw_2026",
        events="OPEN 2026-09-08",
    )
    values.update(overrides)
    return values


def test_parse_occ_symbol():
    assert parse_occ_symbol("ONON  260918P00027500") == ("ONON", date(2026, 9, 18), Decimal("27.50"), "PUT")
    assert parse_occ_symbol("BRK.B 261016C00500000") == ("BRK.B", date(2026, 10, 16), Decimal("500.00"), "CALL")
    assert parse_occ_symbol("ONON") is None


def test_classify_sell_to_open_expire_and_assignment():
    txns = [
        _option_txn(1, "2026-09-08", "ONON  260918P00027500", -2, 0.25, effect="OPENING"),
        _option_txn(2, "2026-09-19", "ONON  260918P00027500", 1, 0, txn_type="RECEIVE_AND_DELIVER",
                    description="EXPIRATION"),
        _option_txn(3, "2026-09-19", "ONON  260918P00027500", 1, 0, txn_type="RECEIVE_AND_DELIVER",
                    description="ASSIGNMENT",
                    extra=[{"instrument": {"assetType": "EQUITY", "symbol": "ONON"}, "amount": 100, "price": 27.5}]),
    ]
    options, equities, unclassified = classify_legs(txns)
    assert [o.action for o in options] == ["SELL_TO_OPEN", "EXPIRE", "ASSIGN"]
    assert options[0].contracts == Decimal("2")
    assert equities == []  # the assignment's share delivery is not a separate buy
    assert unclassified == []


def test_buy_without_position_effect_is_flagged_not_guessed():
    options, _, _ = classify_legs([_option_txn(1, "2026-09-08", "LULU  260918P00140000", 1, 0.1)])
    assert options[0].action == "BUY (open or close?)"


def test_unparseable_option_symbol_is_unclassified():
    _, _, unclassified = classify_legs([_option_txn(1, "2026-09-08", "WEIRD", -1, 0.1)])
    assert len(unclassified) == 1


def test_compare_reports_missing_from_db_and_okw():
    options, equities, _ = classify_legs(
        [_option_txn(1, "2026-09-10", "ADBE  260925C00240000", -1, 3.9, effect="OPENING")]
    )
    report = compare(options, equities, [], [], date(2026, 9, 1), okw_keys=set())
    assert any(line.startswith("[missing_in_okw] ADBE $240.00 CALL") for line in report)
    assert any(line.startswith("[missing_in_db] ADBE $240.00 CALL") for line in report)


def test_compare_matching_trade_is_clean():
    options, equities, _ = classify_legs(
        [_option_txn(1, "2026-09-08", "ONON  260918P00027500", -1, 0.25, effect="OPENING")]
    )
    okw = {("ONON", date(2026, 9, 18), Decimal("27.50"), "PUT")}
    assert compare(options, equities, [_db_trade()], [], date(2026, 9, 1), okw_keys=okw) == []


def test_compare_flags_contract_count_price_and_outcome():
    options, equities, _ = classify_legs(
        [
            _option_txn(1, "2026-09-08", "ONON  260918P00027500", -2, 0.21, effect="OPENING"),
            _option_txn(2, "2026-09-19", "ONON  260918P00027500", 2, 0, txn_type="RECEIVE_AND_DELIVER",
                        description="EXPIRATION"),
        ]
    )
    report = compare(options, equities, [_db_trade()], [], date(2026, 9, 1))
    checks = {line.split("]")[0] + "]" for line in report}
    assert checks == {"[contract_count]", "[price]", "[outcome]"}


def test_compare_flags_db_trade_without_schwab_fill_and_missing_equity():
    txn = {
        "activityId": 9,
        "tradeDate": "2026-09-15T14:30:00+0000",
        "type": "TRADE",
        "transferItems": [{"instrument": {"assetType": "EQUITY", "symbol": "CRM"}, "amount": 100, "price": 185.75}],
    }
    options, equities, _ = classify_legs([txn])
    report = compare(options, equities, [_db_trade()], [], date(2026, 9, 1))
    assert any(line.startswith("[no_schwab_fill] DB #2000") for line in report)
    assert any(line.startswith("[equity_missing_in_db]") and "BTO CRM 100" in line for line in report)


def test_spread_is_compared_on_net_credit_and_long_leg_is_not_a_count_mismatch():
    options, equities, _ = classify_legs(
        [
            _option_txn(1, "2026-09-09", "ANET  261218P00130000", -1, 1.84, effect="OPENING"),
            _option_txn(2, "2026-09-09", "ANET  261218P00125000", 1, 1.44, effect="OPENING"),
        ]
    )
    spread = _db_trade(id=1834, ticker="ANET", trade_type="ROCS BULL PUT SPREAD", date_trade_open=date(2026, 9, 9),
                       expiration_date=date(2026, 12, 18), strike_price=Decimal("130.00"),
                       long_strike=Decimal("125.00"), credit_debit=Decimal("0.4000"))
    assert compare(options, equities, [spread], [], date(2026, 9, 1)) == []


def test_assignment_delivery_is_matched_to_the_assigned_put():
    txn = {
        "activityId": 7,
        "tradeDate": "2026-09-21T14:30:00+0000",
        "type": "TRADE",
        "description": "FISERV INC",
        "transferItems": [{"instrument": {"assetType": "EQUITY", "symbol": "FISV"}, "amount": 100, "price": 47.5}],
    }
    options, equities, _ = classify_legs([txn])
    put = _db_trade(id=1800, ticker="FISV", date_trade_open=date(2026, 9, 10), expiration_date=date(2026, 9, 18),
                    strike_price=Decimal("47.50"), events="OPEN 2026-09-10")
    report = compare(options, equities, [put], [], date(2026, 9, 1))
    assert report[-1].startswith("[assignment_not_recorded] 2026-09-21 100 FISV")
    put["events"] = "OPEN 2026-09-10, ASSIGN 2026-09-18"
    report = compare(options, equities, [put], [], date(2026, 9, 1))
    assert not any("assignment" in line or "equity_missing" in line for line in report)
