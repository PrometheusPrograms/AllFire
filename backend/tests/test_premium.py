"""Tests for app.services.premium."""

from datetime import date
from decimal import Decimal

import pytest

from app.services.premium import (
    average_weekly_premium,
    bucket_premium,
    premium_for_trade,
    total_premium,
    week_bounds,
    weeks_elapsed,
)


def test_premium_for_trade_options_credit():
    premium = premium_for_trade(
        trade_type_category="OPTIONS",
        is_credit=True,
        net_credit_per_share=Decimal("0.83"),
        num_of_contracts=5,
    )
    assert premium == Decimal("415.00")


def test_premium_for_trade_none_for_stock_trades():
    assert (
        premium_for_trade(
            trade_type_category="STOCK",
            is_credit=True,
            net_credit_per_share=Decimal("10"),
            num_of_contracts=1,
        )
        is None
    )


def test_premium_for_trade_none_for_debit_options():
    assert (
        premium_for_trade(
            trade_type_category="OPTIONS",
            is_credit=False,
            net_credit_per_share=Decimal("10"),
            num_of_contracts=1,
        )
        is None
    )


def test_premium_for_trade_none_when_missing_fields():
    assert (
        premium_for_trade(
            trade_type_category="OPTIONS",
            is_credit=True,
            net_credit_per_share=None,
            num_of_contracts=1,
        )
        is None
    )


def test_week_bounds_returns_monday_to_sunday():
    # Thursday, Aug 27, 2026 -> week is Mon Aug 24 - Sun Aug 30, 2026.
    bounds = week_bounds(date(2026, 8, 27))
    assert bounds.start == date(2026, 8, 24)
    assert bounds.end == date(2026, 8, 30)


def test_week_bounds_when_date_is_monday():
    bounds = week_bounds(date(2026, 8, 24))
    assert bounds.start == date(2026, 8, 24)
    assert bounds.end == date(2026, 8, 30)


def test_weeks_elapsed_inclusive_of_both_ends():
    # Jan 1 - Jan 7 inclusive = 7 days = exactly 1 week.
    assert weeks_elapsed(date(2026, 1, 1), date(2026, 1, 7)) == Decimal("1")


def test_weeks_elapsed_fractional():
    # Jan 1 - Jan 4 inclusive = 4 days = 4/7 week.
    assert weeks_elapsed(date(2026, 1, 1), date(2026, 1, 4)) == Decimal("4") / Decimal("7")


def test_weeks_elapsed_inverted_range_returns_zero():
    assert weeks_elapsed(date(2026, 1, 7), date(2026, 1, 1)) == Decimal("0")


def test_total_premium_filters_to_range():
    entries = [
        (date(2026, 1, 1), Decimal("100")),
        (date(2026, 1, 10), Decimal("200")),
        (date(2026, 2, 1), Decimal("300")),
    ]
    assert total_premium(entries, date(2026, 1, 1), date(2026, 1, 31)) == Decimal("300")


def test_average_weekly_premium_ytd_example():
    entries = [
        (date(2026, 1, 1), Decimal("70")),
        (date(2026, 1, 8), Decimal("70")),
    ]
    # Jan 1 - Jan 14 inclusive = 14 days = 2 weeks; total 140 / 2 = 70/week.
    avg = average_weekly_premium(entries, date(2026, 1, 1), date(2026, 1, 14))
    assert avg == Decimal("70")


def test_average_weekly_premium_empty_range_returns_zero():
    assert average_weekly_premium([], date(2026, 1, 1), date(2026, 1, 1)) is not None
    assert average_weekly_premium([], date(2026, 1, 7), date(2026, 1, 1)) == Decimal("0")


def test_bucket_premium_weekly_includes_empty_weeks():
    entries = [
        (date(2026, 1, 1), Decimal("100")),  # week of Dec 29 - Jan 4
        (date(2026, 1, 20), Decimal("50")),  # week of Jan 19 - Jan 25
    ]
    buckets = bucket_premium(entries, date(2026, 1, 1), date(2026, 1, 25), bucket="week")

    assert len(buckets) == 4
    assert buckets[0].premium_total == Decimal("100")
    assert buckets[0].trade_count == 1
    assert buckets[1].premium_total == Decimal("0")
    assert buckets[1].trade_count == 0
    assert buckets[3].premium_total == Decimal("50")
    assert buckets[3].trade_count == 1


def test_bucket_premium_monthly():
    entries = [
        (date(2026, 1, 5), Decimal("100")),
        (date(2026, 1, 20), Decimal("50")),
        (date(2026, 2, 3), Decimal("25")),
    ]
    buckets = bucket_premium(entries, date(2026, 1, 1), date(2026, 2, 28), bucket="month")

    assert len(buckets) == 2
    assert buckets[0].period_start == date(2026, 1, 1)
    assert buckets[0].period_end == date(2026, 1, 31)
    assert buckets[0].premium_total == Decimal("150")
    assert buckets[0].trade_count == 2
    assert buckets[1].period_start == date(2026, 2, 1)
    assert buckets[1].period_end == date(2026, 2, 28)
    assert buckets[1].premium_total == Decimal("25")


def test_bucket_premium_invalid_bucket_raises():
    with pytest.raises(ValueError):
        bucket_premium([], date(2026, 1, 1), date(2026, 1, 2), bucket="year")


def test_bucket_premium_day_bucket():
    entries = [(date(2026, 1, 1), Decimal("10")), (date(2026, 1, 2), Decimal("20"))]
    buckets = bucket_premium(entries, date(2026, 1, 1), date(2026, 1, 2), bucket="day")
    assert len(buckets) == 2
    assert buckets[0].period_start == buckets[0].period_end == date(2026, 1, 1)
    assert buckets[0].premium_total == Decimal("10")
    assert buckets[1].premium_total == Decimal("20")


def test_bucket_premium_inverted_range_returns_empty_list():
    assert bucket_premium([], date(2026, 1, 10), date(2026, 1, 1)) == []
