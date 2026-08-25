"""Tests for app.services.rorc.

The primary case reproduces OKW_2026.xlsx's own "ARORC Calculator" sheet
worked example exactly (Strike 217.5, Premium 0.83, Commission 0.0041,
DTE 7), confirming the pure functions match the spreadsheet's cell
formulas bit for bit.
"""

from decimal import Decimal

import pytest

from app.services.rorc import calculate_arorc, calculate_rorc


def test_rorc_matches_spreadsheet_worked_example():
    strike = Decimal("217.5")
    premium = Decimal("0.83")
    commission = Decimal("0.0041")
    net_credit_per_share = premium - commission
    risk_capital_per_share = strike - net_credit_per_share

    rorc = calculate_rorc(net_credit_per_share, risk_capital_per_share)

    assert risk_capital_per_share == Decimal("216.6741")
    assert rorc == net_credit_per_share / risk_capital_per_share
    assert rorc.quantize(Decimal("0.0000001")) == Decimal("0.0038117")


def test_arorc_matches_spreadsheet_worked_example():
    rorc = Decimal("0.0038117153826876396")
    days_to_expiration = 7

    arorc = calculate_arorc(rorc, days_to_expiration)

    # Spreadsheet's own computed ARORC for this example is 0.19875373066871266.
    assert arorc.quantize(Decimal("0.00000001")) == Decimal("0.19875373")


def test_rorc_applies_margin_percent():
    net_credit_per_share = Decimal("1.00")
    risk_capital_per_share = Decimal("100.00")

    full_cash = calculate_rorc(net_credit_per_share, risk_capital_per_share)
    half_margin = calculate_rorc(
        net_credit_per_share, risk_capital_per_share, margin_percent=Decimal("50")
    )

    assert full_cash == Decimal("0.01")
    # Half the capital at risk doubles the return on that capital.
    assert half_margin == Decimal("0.02")


def test_rorc_zero_risk_capital_returns_zero():
    assert calculate_rorc(Decimal("1.00"), Decimal("0")) == Decimal("0")


def test_rorc_negative_risk_capital_returns_zero():
    assert calculate_rorc(Decimal("1.00"), Decimal("-5")) == Decimal("0")


def test_arorc_zero_days_returns_zero():
    assert calculate_arorc(Decimal("0.05"), 0) == Decimal("0")


def test_arorc_negative_days_returns_zero():
    assert calculate_arorc(Decimal("0.05"), -3) == Decimal("0")


@pytest.mark.parametrize("days", [1, 7, 30, 365])
def test_arorc_scales_inversely_with_days(days):
    rorc = Decimal("0.01")
    arorc = calculate_arorc(rorc, days)
    assert arorc == rorc * (Decimal("365") / Decimal(days))
