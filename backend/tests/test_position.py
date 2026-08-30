"""Tests for app.services.position."""

from decimal import Decimal

from app.services.position import (
    basis_per_share,
    classify_share_class,
    fold_amount_ledger,
    reduced_cost_basis,
    summarize_positions,
)


def test_classify_share_class_roct_is_trading():
    assert classify_share_class("ROCT PUT") == "trading"
    assert classify_share_class("ROCT CALL") == "trading"


def test_classify_share_class_rocs_is_trading():
    assert classify_share_class("ROCS BULL PUT SPREAD") == "trading"


def test_classify_share_class_rule_one_is_long_term():
    assert classify_share_class("RULE ONE PUT") == "long_term"
    assert classify_share_class("RULE ONE CALL") == "long_term"


def test_classify_share_class_bto_stc_default_long_term():
    assert classify_share_class("BTO") == "long_term"
    assert classify_share_class("STC") == "long_term"


def test_classify_share_class_none_defaults_long_term():
    assert classify_share_class(None) == "long_term"


def test_classify_share_class_case_insensitive():
    assert classify_share_class("roct put") == "trading"


def test_summarize_positions_empty():
    totals = summarize_positions([])
    assert totals.total.shares == Decimal("0")
    assert totals.total.cost_basis == Decimal("0")
    assert totals.total.avg_cost_per_share is None
    assert totals.trading.avg_cost_per_share is None
    assert totals.long_term.avg_cost_per_share is None


def test_summarize_positions_splits_trading_and_long_term():
    entries = [
        ("trading", Decimal("100"), Decimal("4500")),  # ROCT PUT assignment
        ("long_term", Decimal("200"), Decimal("9000")),  # RULE ONE PUT assignment
        ("long_term", Decimal("50"), Decimal("2250")),  # BTO
    ]
    totals = summarize_positions(entries)

    assert totals.trading.shares == Decimal("100")
    assert totals.trading.cost_basis == Decimal("4500")
    assert totals.trading.avg_cost_per_share == Decimal("45")

    assert totals.long_term.shares == Decimal("250")
    assert totals.long_term.cost_basis == Decimal("11250")
    assert totals.long_term.avg_cost_per_share == Decimal("45")

    assert totals.total.shares == Decimal("350")
    assert totals.total.cost_basis == Decimal("15750")
    assert totals.total.avg_cost_per_share == Decimal("45")


def test_summarize_positions_accounts_for_sold_shares():
    entries = [
        ("trading", Decimal("100"), Decimal("4500")),  # assigned in
        ("trading", Decimal("-100"), Decimal("-4700")),  # called away (CALL assignment)
    ]
    totals = summarize_positions(entries)

    assert totals.trading.shares == Decimal("0")
    assert totals.trading.cost_basis == Decimal("-200")
    # Zero shares held -> avg cost per share is undefined, not a div-by-zero error.
    assert totals.trading.avg_cost_per_share is None


def test_basis_per_share_matches_spreadsheet_sign():
    """Rule One Basis Reduction: Basis/sh = (costs − premiums) / shares.

    RIVN header (Rule 1 sheet): 100 shares assigned at $10, then $1,086 of
    collected premium → BASIS −86, Basis/sh −0.86.
    """
    assert reduced_cost_basis(Decimal("1000"), Decimal("1086")) == Decimal("-86")
    assert basis_per_share(Decimal("1000"), Decimal("1086"), Decimal("100")) == Decimal("-0.86")
    assert basis_per_share(Decimal("1000"), Decimal("42"), Decimal("0")) is None


def test_template_sheet_amount_ledger_replays_to_header_basis():
    """Golden numbers from the Template sheet (ATW) in
    `Rule One Basis Reduction spreadsheet.xlsx`.

    Purchases add +Amount and +shares; sold calls/puts add −Amount only;
    share sales add −shares and −proceeds. Header Basis (−0.005465625) is
    the final BASIS (−17.49) / remaining shares (3200).
    """
    rows = [
        (Decimal("1400"), Decimal("9688")),
        (Decimal("1000"), Decimal("6190")),
        (Decimal("3000"), Decimal("20250")),
        (Decimal("450"), Decimal("2777")),
        (Decimal("0"), Decimal("-2948")),
        (Decimal("0"), Decimal("-1400")),
        (Decimal("0"), Decimal("-1974.49")),
        (Decimal("0"), Decimal("-1449")),
        (Decimal("0"), Decimal("-775")),
        (Decimal("0"), Decimal("-111")),
        (Decimal("0"), Decimal("-336")),
        (Decimal("0"), Decimal("-999")),
        (Decimal("-200"), Decimal("-1982")),
        (Decimal("-450"), Decimal("-6293")),
        (Decimal("-300"), Decimal("-2983")),
        (Decimal("-1500"), Decimal("-14982")),
        (Decimal("-200"), Decimal("-2690")),
    ]
    shares, basis, per_share = fold_amount_ledger(rows)
    assert shares == Decimal("3200")
    assert basis == Decimal("-17.49")
    assert per_share == Decimal("-17.49") / Decimal("3200")


def test_rivn_rule1_sheet_amount_ledger_replays_to_header_basis():
    """Golden numbers from the RIVN sheet (Rule 1) in the same workbook.

    One assignment (100 sh @ $10 = +1000) then a series of sold puts/calls
    whose Amounts are negative premiums. Header: 100 shares, Basis −0.86.
    """
    premiums = [
        -42, -58, -43, -46, -10, 380, -43, -48, -68, -32, -50, -60, -32,
        -150, -120, -230, -10, -14, -60, -68, -54, -35, -120, -14, -7,
        -11, -12, -15, -14,
    ]
    rows = [(Decimal("100"), Decimal("1000"))] + [
        (Decimal("0"), Decimal(str(p))) for p in premiums
    ]
    shares, basis, per_share = fold_amount_ledger(rows)
    assert shares == Decimal("100")
    assert basis == Decimal("-86")
    assert per_share == Decimal("-0.86")
