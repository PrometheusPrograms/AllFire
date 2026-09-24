from datetime import date
from decimal import Decimal

from app.services.roll_chain import (
    ChainSpec,
    RollLinkRow,
    build_chains,
    chain_arorc,
    classify_roll,
    format_strike_path,
    match_roll_parents,
)


def _row(**overrides) -> RollLinkRow:
    defaults = dict(
        id=1,
        account_id=1,
        ticker="ADBE",
        trade_type="ROCT PUT",
        num_of_contracts=1,
        date_trade_open=date(2026, 2, 10),
        expiration_date=date(2026, 2, 27),
        strike_price=Decimal("270.00"),
        trade_parent_id=None,
        latest_event="ROLL",
        roll_date=date(2026, 2, 23),
    )
    defaults.update(overrides)
    return RollLinkRow(**defaults)


def test_classify_roll_uses_schwab_names():
    assert (
        classify_roll(
            from_strike=Decimal("36.00"),
            from_expiration=date(2026, 7, 31),
            to_strike=Decimal("34.50"),
            to_expiration=date(2026, 8, 7),
        )
        == "diagonal"
    )
    assert (
        classify_roll(
            from_strike=Decimal("36.00"),
            from_expiration=date(2026, 7, 31),
            to_strike=Decimal("34.50"),
            to_expiration=date(2026, 7, 31),
        )
        == "vertical"
    )
    assert (
        classify_roll(
            from_strike=Decimal("36.00"),
            from_expiration=date(2026, 7, 31),
            to_strike=Decimal("36.00"),
            to_expiration=date(2026, 8, 7),
        )
        == "calendar"
    )


def test_format_strike_path_skips_single_and_nulls():
    assert format_strike_path([Decimal("36")]) is None
    assert format_strike_path([Decimal("36.00"), Decimal("34.50")]) == "36.00 → 34.50"


def test_build_chains_walks_parent_pointer():
    chains = build_chains({10: None, 11: 10, 12: 11, 99: None})
    by_root = {c.root_id: c for c in chains}
    assert by_root[10] == ChainSpec(ids=(10, 11, 12))
    assert by_root[99] == ChainSpec(ids=(99,))


def test_build_chains_orphans_when_parent_filtered_out():
    chains = build_chains({11: 10})
    assert chains == [ChainSpec(ids=(11,))]


def test_match_picks_closest_strike_on_the_roll_date():
    parent = _row(id=70, strike_price=Decimal("270"), roll_date=date(2026, 2, 23))
    continuation = _row(
        id=71,
        date_trade_open=date(2026, 2, 23),
        strike_price=Decimal("265"),
        latest_event="ROLL",
        roll_date=date(2026, 3, 25),
    )
    unrelated = _row(
        id=103,
        date_trade_open=date(2026, 2, 23),
        strike_price=Decimal("225"),
        latest_event="EXPIRE",
        roll_date=None,
    )
    assert match_roll_parents([parent, continuation, unrelated]) == [(71, 70)]


def test_match_skips_already_parented_and_already_has_child():
    parent = _row(id=1)
    child = _row(
        id=2,
        date_trade_open=date(2026, 2, 23),
        strike_price=Decimal("265"),
        trade_parent_id=1,
        latest_event="EXPIRE",
        roll_date=None,
    )
    assert match_roll_parents([parent, child]) == []


def test_match_uses_slack_when_open_is_a_few_days_after_roll():
    parent = _row(
        id=119,
        ticker="LULU",
        date_trade_open=date(2026, 3, 6),
        strike_price=Decimal("300"),
        roll_date=date(2026, 3, 11),
    )
    child = _row(
        id=120,
        ticker="LULU",
        date_trade_open=date(2026, 3, 13),
        strike_price=Decimal("295"),
        latest_event="EXPIRE",
        roll_date=None,
    )
    assert match_roll_parents([parent, child]) == [(120, 119)]


def test_match_pairs_two_same_day_rolls_by_strike_then_expiration():
    """RBLX 31 JUL 26: two ROLLs and two 34.50 continuations the same day."""
    first = _row(
        id=500,
        ticker="RBLX",
        date_trade_open=date(2026, 7, 22),
        expiration_date=date(2026, 7, 31),
        strike_price=Decimal("36.00"),
        roll_date=date(2026, 7, 31),
    )
    second = _row(
        id=522,
        ticker="RBLX",
        date_trade_open=date(2026, 7, 30),
        expiration_date=date(2026, 7, 31),
        strike_price=Decimal("36.50"),
        roll_date=date(2026, 7, 31),
    )
    week_out = _row(
        id=501,
        ticker="RBLX",
        date_trade_open=date(2026, 7, 31),
        expiration_date=date(2026, 8, 7),
        strike_price=Decimal("34.50"),
        latest_event="EXPIRE",
        roll_date=None,
    )
    two_weeks = _row(
        id=523,
        ticker="RBLX",
        date_trade_open=date(2026, 7, 31),
        expiration_date=date(2026, 8, 14),
        strike_price=Decimal("34.50"),
        latest_event="EXPIRE",
        roll_date=None,
    )
    links = set(match_roll_parents([first, second, week_out, two_weeks]))
    assert links == {(501, 500), (523, 522)}


def test_chain_arorc_sums_net_credit_over_calendar_span():
    # 22 JUL -> 7 AUG = 16 days; NC 0.2059 + 0.0159; RC 35.7941
    # Spreadsheet 100% cash-secured is stored as 1, same as trades.margin_percent.
    result = chain_arorc(
        date_trade_open=date(2026, 7, 22),
        expiration_date=date(2026, 8, 7),
        net_credits=[Decimal("0.2059"), Decimal("0.0159")],
        risk_capital_per_share=Decimal("35.7941"),
        margin_percent=Decimal("1"),
    )
    assert result is not None
    expected_rorc = Decimal("0.2218") / Decimal("35.7941")
    expected = expected_rorc * Decimal("365") / Decimal("16")
    assert result.quantize(Decimal("0.000001")) == expected.quantize(Decimal("0.000001"))
