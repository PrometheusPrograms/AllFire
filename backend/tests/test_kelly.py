"""Tests for app.services.kelly."""

from decimal import Decimal

from app.services.kelly import calculate_kelly


def test_kelly_basic_case():
    # W=0.7, avg_win=2, avg_loss=1 -> R=2 -> Kelly = 0.7 - (0.3/2) = 0.55
    result = calculate_kelly(
        probability_of_winning=Decimal("0.7"),
        avg_win=Decimal("2"),
        avg_loss=Decimal("1"),
    )
    assert result == Decimal("0.55")


def test_kelly_even_odds_breakeven_probability():
    # R=1 (even odds), W=0.5 -> Kelly = 0.5 - (0.5/1) = 0
    result = calculate_kelly(
        probability_of_winning=Decimal("0.5"),
        avg_win=Decimal("1"),
        avg_loss=Decimal("1"),
    )
    assert result == Decimal("0")


def test_kelly_high_win_rate_favorable_odds():
    # Matches the spreadsheet's "Templates" sheet Kelly section shape:
    # W=0.85, avg_win=0.83 (net credit per share), avg_loss=65.0 (risk
    # capital * 30% at-risk assumption) -> small but positive Kelly fraction.
    result = calculate_kelly(
        probability_of_winning=Decimal("0.85"),
        avg_win=Decimal("0.83"),
        avg_loss=Decimal("65.0"),
    )
    win_loss_ratio = Decimal("0.83") / Decimal("65.0")
    expected = Decimal("0.85") - (Decimal("1") - Decimal("0.85")) / win_loss_ratio
    assert result == expected
    assert result < Decimal("0")  # thin edge relative to the loss size — negative Kelly


def test_kelly_zero_avg_loss_returns_zero():
    result = calculate_kelly(
        probability_of_winning=Decimal("0.6"),
        avg_win=Decimal("1"),
        avg_loss=Decimal("0"),
    )
    assert result == Decimal("0")


def test_kelly_negative_avg_loss_returns_zero():
    result = calculate_kelly(
        probability_of_winning=Decimal("0.6"),
        avg_win=Decimal("1"),
        avg_loss=Decimal("-1"),
    )
    assert result == Decimal("0")


def test_kelly_zero_avg_win_returns_zero():
    # R = 0/avg_loss = 0 -> undefined win/loss ratio -> Kelly falls back to 0.
    result = calculate_kelly(
        probability_of_winning=Decimal("0.6"),
        avg_win=Decimal("0"),
        avg_loss=Decimal("1"),
    )
    assert result == Decimal("0")


def test_kelly_certain_win_returns_full_probability():
    # W=1 -> (1-W) term vanishes regardless of R -> Kelly = 1.
    result = calculate_kelly(
        probability_of_winning=Decimal("1"),
        avg_win=Decimal("1"),
        avg_loss=Decimal("1"),
    )
    assert result == Decimal("1")
