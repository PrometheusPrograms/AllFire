"""Premium-income calculations for the trades dashboard.

"Premium received" only applies to credit options trades (`ROCT PUT/CALL`,
`RULE ONE PUT/CALL`, `ROCS BULL PUT SPREAD`) — plain stock trades (`BTO`/
`STC`) move cost basis, not premium income, so they're excluded here rather
than in the caller. Counted on the day the trade was opened (when the
premium was actually collected), not any later closing date.

Kept decoupled from the ORM/DB on purpose, like `rorc.py`/`cost_basis.py`:
callers assemble `(date, Decimal)` entries from `trades` rows (via
`premium_for_trade` per row) and hand them to the bucketing/averaging
functions here, so this stays trivially unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Sequence

ZERO = Decimal("0")
DAYS_PER_WEEK = Decimal("7")
OPTIONS_CONTRACT_MULTIPLIER = Decimal("100")

PremiumEntry = tuple[date, Decimal]


def premium_for_trade(
    *,
    trade_type_category: str | None,
    is_credit: bool | None,
    net_credit_per_share: Decimal | None,
    num_of_contracts: int | None,
) -> Decimal | None:
    """Premium actually collected when this trade was opened, or `None` if
    it's not a premium-generating trade (wrong category/direction) or is
    missing a field needed to compute it (e.g. a covered call with
    `margin_capital` recorded as "COVERED" text upstream — see
    `scripts/okw_parser.py` — can still be missing other numeric fields).
    """
    if trade_type_category != "OPTIONS" or not is_credit:
        return None
    if net_credit_per_share is None or num_of_contracts is None:
        return None
    return net_credit_per_share * Decimal(num_of_contracts) * OPTIONS_CONTRACT_MULTIPLIER


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date


def week_bounds(any_date: date) -> DateRange:
    """Monday-Sunday range containing `any_date`."""
    start = any_date - timedelta(days=any_date.weekday())
    return DateRange(start=start, end=start + timedelta(days=6))


def weeks_elapsed(start: date, end: date) -> Decimal:
    """Fractional number of weeks in `[start, end]`, inclusive of both ends.

    Fractional (not "count complete Mon-Sun weeks") so a partial current
    week still contributes its share to a YTD/trailing-12-months average,
    rather than being dropped and skewing the average upward.
    """
    if end < start:
        return ZERO
    days = (end - start).days + 1
    return Decimal(days) / DAYS_PER_WEEK


def total_premium(entries: Sequence[PremiumEntry], start: date, end: date) -> Decimal:
    """Sum of premium entries whose date falls within `[start, end]`."""
    return sum(
        (amount for entry_date, amount in entries if start <= entry_date <= end),
        start=ZERO,
    )


def average_weekly_premium(entries: Sequence[PremiumEntry], start: date, end: date) -> Decimal:
    """Total premium in the period divided by the number of weeks elapsed.

    Returns `Decimal("0")` for a degenerate (empty or inverted) range,
    rather than raising on division by zero.
    """
    weeks = weeks_elapsed(start, end)
    if weeks <= ZERO:
        return ZERO
    return total_premium(entries, start, end) / weeks


@dataclass(frozen=True)
class PeriodTotal:
    period_start: date
    period_end: date
    premium_total: Decimal
    trade_count: int


def _day_bounds(any_date: date) -> DateRange:
    return DateRange(start=any_date, end=any_date)


def _month_bounds(any_date: date) -> DateRange:
    start = any_date.replace(day=1)
    if start.month == 12:
        next_month_start = start.replace(year=start.year + 1, month=1)
    else:
        next_month_start = start.replace(month=start.month + 1)
    return DateRange(start=start, end=next_month_start - timedelta(days=1))


def bucket_premium(
    entries: Sequence[PremiumEntry],
    start: date,
    end: date,
    bucket: str = "week",
) -> list[PeriodTotal]:
    """Partition `[start, end]` into consecutive day, week, or month buckets
    and sum premium entries falling into each one — the series the dashboard
    chart plots. Buckets with no trades are still included (as zero), so
    the chart doesn't silently skip quiet days/weeks/months.
    """
    if bucket not in ("day", "week", "month"):
        raise ValueError(f"bucket must be 'day', 'week', or 'month', got {bucket!r}")
    if end < start:
        return []

    bounds_fn = {"day": _day_bounds, "week": week_bounds, "month": _month_bounds}[bucket]
    step = {"day": timedelta(days=1), "week": timedelta(days=7), "month": None}[bucket]

    periods: list[DateRange] = []
    cursor = bounds_fn(start).start
    while cursor <= end:
        period = bounds_fn(cursor)
        periods.append(period)
        cursor = period.end + timedelta(days=1) if step is None else cursor + step

    results = []
    for period in periods:
        matching = [
            amount for entry_date, amount in entries if period.start <= entry_date <= period.end
        ]
        results.append(
            PeriodTotal(
                period_start=period.start,
                period_end=period.end,
                premium_total=sum(matching, start=ZERO),
                trade_count=len(matching),
            )
        )
    return results
