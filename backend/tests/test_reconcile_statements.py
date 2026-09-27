"""Tests for statement vs OKW reconciliation (synthetic rows, no private PDFs)."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from scripts.reconcile_statements import (
    DEFAULT_LULU_FIXTURE,
    EquityMove,
    OptionFill,
    check_expected_fixture,
    classify_statement_row,
    load_expected_lots,
    parse_option_identity,
    reconcile_equity_shares,
    reconcile_options,
    statement_walk_share_total,
    tos_lot_share_total,
)
from scripts.schwab_statement_parser import TxnRow


def test_lulu_expected_fixture_sums_to_900():
    expected = load_expected_lots(DEFAULT_LULU_FIXTURE)
    assert expected.expected_shares == 900
    assert tos_lot_share_total(expected) == 900
    assert statement_walk_share_total(expected) == 900
    assert check_expected_fixture(expected) == []
    assert len(expected.tos_lots) == 13


def test_parse_option_symbol_from_statement_cusip():
    ident = parse_option_identity("LULU09/13/2024245.00P", "")
    assert ident == ("LULU", date(2024, 9, 13), Decimal("245.00"), "PUT")

    ident = parse_option_identity("LULU05/29/2026126.00C", "")
    assert ident == ("LULU", date(2026, 5, 29), Decimal("126.00"), "CALL")

    ident = parse_option_identity("LULU06/26/202698.00EXP06/26/26P", "")
    assert ident == ("LULU", date(2026, 6, 26), Decimal("98.00"), "PUT")

    assert parse_option_identity("LULU", "LULULEMONATHLETICAINC") is None


def test_parse_option_symbol_layouts_seen_in_2024_2026_statements():
    assert parse_option_identity("ONON03/13/202638.0003/13/26P", "") == (
        "ONON",
        date(2026, 3, 13),
        Decimal("38.00"),
        "PUT",
    )
    assert parse_option_identity("LYFT05/08/2026PUTLYFTINC11.50P", "") == (
        "LYFT",
        date(2026, 5, 8),
        Decimal("11.50"),
        "PUT",
    )
    assert parse_option_identity("COIN11/14/2025PUTCOINBASEGLOBALINC$260260.00P", "") == (
        "COIN",
        date(2025, 11, 14),
        Decimal("260.00"),
        "PUT",
    )
    assert parse_option_identity("TSLA10/03/2025CALLTESLAINC487.50C", "") == (
        "TSLA",
        date(2025, 10, 3),
        Decimal("487.50"),
        "CALL",
    )


def _option_row(**overrides) -> TxnRow:
    values = dict(
        date=date(2026, 3, 9),
        category="Sale",
        action=None,
        symbol_cusip="ONON03/13/202638.0003/13/26P",
        description="PUTONHLDGAG",
        quantity=None,
        price=Decimal("0.2000"),
        charges=Decimal("0.83"),
        amount=Decimal("39.17"),
        realized_gain=None,
        raw_lines=["Sale ONON PUTONHLDGAG $38 EXP (2.0000) 0.2000 0.83 39.17"],
    )
    values.update(overrides)
    return TxnRow(**values)


def test_missing_quantity_is_read_from_the_raw_line_not_defaulted_to_one():
    fill = classify_statement_row(_option_row(), "ONON")
    assert fill.contracts == Decimal("2.0000")


def test_missing_quantity_falls_back_to_amount_fees_and_price():
    fill = classify_statement_row(_option_row(raw_lines=[]), "ONON")
    assert fill.contracts == Decimal("2")  # (39.17 + 0.83) / (0.20 * 100)


def test_option_row_with_no_recoverable_quantity_is_skipped():
    row = _option_row(raw_lines=[], amount=None, category="Other", action="ExpiredLong")
    assert classify_statement_row(row, "ONON") is None


def test_duplicate_statement_download_is_read_once(tmp_path, monkeypatch):
    from scripts import reconcile_statements

    for name in (
        "Brokerage Statement_2026-06-30_467.PDF",
        "Brokerage Statement_2026-06-30_467 (1).PDF",
    ):
        (tmp_path / name).write_bytes(b"%PDF")
    meta = SimpleNamespace(period_start=date(2026, 6, 1), period_end=date(2026, 6, 30))
    monkeypatch.setattr(
        reconcile_statements,
        "parse_statement",
        lambda path: SimpleNamespace(meta=meta, transactions=[]),
    )
    assert len(reconcile_statements.parse_statement_dir(tmp_path, "467")) == 1


def test_classify_equity_and_option_rows():
    buy = TxnRow(
        date=date(2025, 7, 25),
        category="Purchase",
        action=None,
        symbol_cusip="LULU",
        description="LULULEMONATHLETICAINC",
        quantity=Decimal("10.0000"),
        price=Decimal("218.99"),
        charges=None,
        amount=Decimal("-2189.90"),
        realized_gain=None,
    )
    move = classify_statement_row(buy, "LULU")
    assert isinstance(move, EquityMove)
    assert move.shares == Decimal("10")

    sto = TxnRow(
        date=date(2024, 9, 5),
        category="Sale",
        action=None,
        symbol_cusip="LULU09/13/2024245.00P",
        description="PUTLULULEMONATHLETICA$245 EXP09/13/24",
        quantity=Decimal("-1.0000"),
        price=Decimal("2.9200"),
        charges=None,
        amount=Decimal("291.53"),
        realized_gain=None,
    )
    fill = classify_statement_row(sto, "LULU")
    assert isinstance(fill, OptionFill)
    assert fill.action == "sto"
    assert fill.strike == Decimal("245.00")
    assert fill.contracts == Decimal("1")


def test_reconcile_equity_flags_share_mismatch():
    equity = [
        EquityMove(date(2025, 7, 25), "LULU", Decimal("10"), Decimal("218.99"), "purchase"),
        EquityMove(date(2025, 7, 30), "LULU", Decimal("10"), Decimal("214.99"), "purchase"),
    ]
    issues = reconcile_equity_shares(equity, Decimal("19"))
    assert any(i.check == "share_count" for i in issues)
    assert reconcile_equity_shares(equity, Decimal("20")) == []


def test_reconcile_options_flags_missing_okw_and_result_mismatch():
    sto = OptionFill(
        txn_date=date(2024, 9, 5),
        ticker="LULU",
        expiration=date(2024, 9, 13),
        strike=Decimal("245.00"),
        right="PUT",
        contracts=Decimal("1"),
        premium=Decimal("2.92"),
        action="sto",
    )
    expire = OptionFill(
        txn_date=date(2024, 9, 16),
        ticker="LULU",
        expiration=date(2024, 9, 13),
        strike=Decimal("245.00"),
        right="PUT",
        contracts=Decimal("1"),
        premium=None,
        action="expire",
    )
    missing = reconcile_options([sto], [], {})
    assert any(i.check == "missing_okw" for i in missing)

    trade = SimpleNamespace(
        id=769,
        trade_type="ROCT PUT",
        expiration_date=date(2024, 9, 13),
        strike_price=Decimal("245.00"),
        num_of_contracts=1,
        date_trade_open=date(2024, 9, 4),
        credit_debit=Decimal("2.92"),
    )
    assign_event = SimpleNamespace(event_type="ASSIGN", event_date=date(2024, 9, 4))
    issues = reconcile_options([sto, expire], [trade], {769: [assign_event]})
    assert any(i.check == "result_mismatch" for i in issues)


def test_reconcile_options_treats_spread_long_leg_expire_as_matched():
    expire = OptionFill(
        txn_date=date(2025, 11, 3),
        ticker="LULU",
        expiration=date(2025, 10, 31),
        strike=Decimal("145.00"),
        right="PUT",
        contracts=Decimal("2"),
        premium=None,
        action="expire",
    )
    spread = SimpleNamespace(
        id=1200,
        trade_type="ROCS BULL PUT SPREAD",
        expiration_date=date(2025, 10, 31),
        strike_price=Decimal("150.00"),
        long_strike=Decimal("145.00"),
        num_of_contracts=2,
        date_trade_open=date(2025, 10, 3),
        credit_debit=Decimal("0.30"),
    )
    expire_event = SimpleNamespace(event_type="EXPIRE", event_date=date(2025, 10, 31))
    assert reconcile_options([expire], [spread], {1200: [expire_event]}) == []


def test_reconcile_options_matches_clean_expire():
    sto = OptionFill(
        txn_date=date(2024, 9, 5),
        ticker="LULU",
        expiration=date(2024, 9, 13),
        strike=Decimal("245.00"),
        right="PUT",
        contracts=Decimal("1"),
        premium=Decimal("2.92"),
        action="sto",
    )
    expire = OptionFill(
        txn_date=date(2024, 9, 16),
        ticker="LULU",
        expiration=date(2024, 9, 13),
        strike=Decimal("245.00"),
        right="PUT",
        contracts=Decimal("1"),
        premium=None,
        action="expire",
    )
    trade = SimpleNamespace(
        id=769,
        trade_type="ROCT PUT",
        expiration_date=date(2024, 9, 13),
        strike_price=Decimal("245.00"),
        num_of_contracts=1,
        date_trade_open=date(2024, 9, 4),
        credit_debit=Decimal("2.92"),
    )
    expire_event = SimpleNamespace(event_type="EXPIRE", event_date=date(2024, 9, 13))
    assert reconcile_options([sto, expire], [trade], {769: [expire_event]}) == []
    expire = OptionFill(
        txn_date=date(2025, 11, 3),
        ticker="LULU",
        expiration=date(2025, 10, 31),
        strike=Decimal("145.00"),
        right="PUT",
        contracts=Decimal("2"),
        premium=None,
        action="expire",
    )
    spread = SimpleNamespace(
        id=1200,
        trade_type="ROCS BULL PUT SPREAD",
        expiration_date=date(2025, 10, 31),
        strike_price=Decimal("150.00"),
        long_strike=Decimal("145.00"),
        num_of_contracts=2,
        date_trade_open=date(2025, 10, 3),
        credit_debit=Decimal("0.30"),
    )
    expire_event = SimpleNamespace(event_type="EXPIRE", event_date=date(2025, 10, 31))
    assert reconcile_options([expire], [spread], {1200: [expire_event]}) == []
    sto = OptionFill(
        txn_date=date(2024, 9, 5),
        ticker="LULU",
        expiration=date(2024, 9, 13),
        strike=Decimal("245.00"),
        right="PUT",
        contracts=Decimal("1"),
        premium=Decimal("2.92"),
        action="sto",
    )
    expire = OptionFill(
        txn_date=date(2024, 9, 16),
        ticker="LULU",
        expiration=date(2024, 9, 13),
        strike=Decimal("245.00"),
        right="PUT",
        contracts=Decimal("1"),
        premium=None,
        action="expire",
    )
    trade = SimpleNamespace(
        id=769,
        trade_type="ROCT PUT",
        expiration_date=date(2024, 9, 13),
        strike_price=Decimal("245.00"),
        num_of_contracts=1,
        date_trade_open=date(2024, 9, 4),
        credit_debit=Decimal("2.92"),
    )
    expire_event = SimpleNamespace(event_type="EXPIRE", event_date=date(2024, 9, 13))
    assert reconcile_options([sto, expire], [trade], {769: [expire_event]}) == []
