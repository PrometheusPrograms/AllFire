"""Dashboard analytics routes: `GET /api/analytics/summary` (KPI strip) and
`GET /api/analytics/premium-timeseries` (the premium-over-time chart).

Both pull raw trade rows into Python and hand them to the pure functions in
`app.services.premium` — the definitions of "premium", "this week", and
"average weekly" live in exactly one place (see that module's docstring),
so the KPI strip and the chart can never silently disagree with each other.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Account, Trade, TradeEvent, TradeType
from app.services.premium import (
    PremiumEntry,
    average_weekly_premium,
    bucket_premium,
    premium_for_trade,
    total_premium,
    week_bounds,
)

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

# Mirrors app.api.trades._display_status_expr: "Open trades" on the KPI
# strip is the same set as the table's Open chip (latest event is not
# ROLL/CLOSE/EXPIRE/ASSIGN, and not a plain stock fill). Live-but-rolled
# options still feed needs-review / expiring-this-week via trade_status.
_current_status = sa.table(
    "v_trade_current_status",
    sa.column("trade_id"),
    sa.column("trade_status"),
)
_latest_event_type = (
    select(TradeEvent.event_type)
    .where(TradeEvent.trade_id == Trade.id)
    .order_by(TradeEvent.event_date.desc(), TradeEvent.id.desc())
    .limit(1)
    .correlate(Trade)
    .scalar_subquery()
)
_CLOSED_OR_ROLLED_EVENTS = ("CLOSE", "EXPIRE", "ASSIGN", "ROLL")


class UpcomingExpiration(BaseModel):
    id: int
    ticker: str
    trade_type: str
    expiration_date: date


class AnalyticsSummaryOut(BaseModel):
    as_of: date
    open_trades_count: int
    avg_arorc_open: Decimal | None
    upcoming_expirations_7d_count: int
    upcoming_expirations_7d: list[UpcomingExpiration]
    needs_review_count: int
    premium_this_week: Decimal
    avg_weekly_premium_ytd: Decimal
    total_premium_ytd: Decimal


class PremiumPeriodOut(BaseModel):
    period_start: date
    period_end: date
    premium_total: Decimal
    trade_count: int


class PremiumTimeseriesOut(BaseModel):
    bucket: str
    items: list[PremiumPeriodOut]


def _base_trade_query(account: str | None):
    query = (
        select(
            Trade.id,
            Trade.ticker,
            Trade.trade_type,
            Trade.date_trade_open,
            Trade.expiration_date,
            Trade.num_of_contracts,
            Trade.arorc,
            Trade.net_credit_per_share,
            TradeType.category.label("trade_type_category"),
            TradeType.is_credit,
            _current_status.c.trade_status,
            _latest_event_type.label("latest_event_type"),
        )
        .select_from(Trade)
        .join(Account, Account.id == Trade.account_id)
        .outerjoin(TradeType, TradeType.id == Trade.trade_type_id)
        .join(_current_status, _current_status.c.trade_id == Trade.id)
    )
    if account:
        query = query.where(Account.account_name == account)
    return query


@router.get("/summary", response_model=AnalyticsSummaryOut)
def get_summary(
    account: str | None = Query(None, description='Account name, e.g. "Rule 1".'),
    as_of: date | None = Query(None, description="Defaults to today."),
    date_from: date | None = Query(
        None, description="If set, open_trades_count only includes date_trade_open >= this."
    ),
    date_to: date | None = Query(
        None, description="If set, open_trades_count only includes date_trade_open <= this."
    ),
    db: Session = Depends(get_db),
) -> AnalyticsSummaryOut:
    as_of = as_of or date.today()
    rows = db.execute(_base_trade_query(account)).mappings().all()

    live_option_rows = [
        r
        for r in rows
        if r["trade_status"] == "open" and r["trade_type_category"] != "STOCK"
    ]
    # Same membership as GET /api/trades?display_status=open.
    display_open_all = [
        r
        for r in live_option_rows
        if r["latest_event_type"] not in _CLOSED_OR_ROLLED_EVENTS
    ]
    display_open_rows = display_open_all
    if date_from is not None:
        display_open_rows = [r for r in display_open_rows if r["date_trade_open"] >= date_from]
    if date_to is not None:
        display_open_rows = [r for r in display_open_rows if r["date_trade_open"] <= date_to]
    open_trades_count = len(display_open_rows)

    open_arorcs = [r["arorc"] for r in display_open_rows if r["arorc"] is not None]
    avg_arorc_open = (
        sum(open_arorcs, start=Decimal("0")) / len(open_arorcs) if open_arorcs else None
    )

    horizon = as_of + timedelta(days=7)
    upcoming = sorted(
        (
            r
            for r in display_open_all
            if r["expiration_date"] is not None and as_of <= r["expiration_date"] <= horizon
        ),
        key=lambda r: r["expiration_date"],
    )
    upcoming_expirations_7d = [
        UpcomingExpiration(
            id=r["id"],
            ticker=r["ticker"],
            trade_type=r["trade_type"],
            expiration_date=r["expiration_date"],
        )
        for r in upcoming
    ]

    # "Needs review": still open per the append-only lifecycle log, but the
    # option/contract has already expired — a data-quality signal (a CLOSE/
    # EXPIRE/ASSIGN event is probably missing), not the old unused
    # Trade.needs_review column.
    # Rolled legs are continuations of another column — their expiration is
    # no longer the live contract, so they are not a missing CLOSE/EXPIRE.
    needs_review_count = sum(
        1
        for r in live_option_rows
        if r["expiration_date"] is not None
        and r["expiration_date"] < as_of
        and r["latest_event_type"] != "ROLL"
    )

    premium_entries: list[PremiumEntry] = []
    for r in rows:
        premium = premium_for_trade(
            trade_type_category=r["trade_type_category"],
            is_credit=r["is_credit"],
            net_credit_per_share=r["net_credit_per_share"],
            num_of_contracts=r["num_of_contracts"],
        )
        if premium is not None:
            premium_entries.append((r["date_trade_open"], premium))

    this_week = week_bounds(as_of)
    premium_this_week = sum(
        (amount for d, amount in premium_entries if this_week.start <= d <= this_week.end),
        start=Decimal("0"),
    )

    ytd_start = date(as_of.year, 1, 1)
    avg_weekly_premium_ytd = average_weekly_premium(premium_entries, ytd_start, as_of)
    total_premium_ytd = total_premium(premium_entries, ytd_start, as_of)

    return AnalyticsSummaryOut(
        as_of=as_of,
        open_trades_count=open_trades_count,
        avg_arorc_open=avg_arorc_open,
        upcoming_expirations_7d_count=len(upcoming_expirations_7d),
        upcoming_expirations_7d=upcoming_expirations_7d,
        needs_review_count=needs_review_count,
        premium_this_week=premium_this_week,
        avg_weekly_premium_ytd=avg_weekly_premium_ytd,
        total_premium_ytd=total_premium_ytd,
    )


@router.get("/premium-timeseries", response_model=PremiumTimeseriesOut)
def get_premium_timeseries(
    start: date = Query(..., description="Inclusive start of the range."),
    end: date = Query(..., description="Inclusive end of the range."),
    account: str | None = Query(None, description='Account name, e.g. "Rule 1".'),
    bucket: str = Query("week", description="'day', 'week', or 'month'."),
    db: Session = Depends(get_db),
) -> PremiumTimeseriesOut:
    if bucket not in ("day", "week", "month"):
        raise HTTPException(400, "bucket must be 'day', 'week', or 'month'")
    if end < start:
        raise HTTPException(400, "end must not be before start")

    rows = db.execute(_base_trade_query(account)).mappings().all()

    premium_entries: list[PremiumEntry] = []
    for r in rows:
        premium = premium_for_trade(
            trade_type_category=r["trade_type_category"],
            is_credit=r["is_credit"],
            net_credit_per_share=r["net_credit_per_share"],
            num_of_contracts=r["num_of_contracts"],
        )
        if premium is not None:
            premium_entries.append((r["date_trade_open"], premium))

    periods = bucket_premium(premium_entries, start, end, bucket=bucket)

    return PremiumTimeseriesOut(
        bucket=bucket,
        items=[
            PremiumPeriodOut(
                period_start=p.period_start,
                period_end=p.period_end,
                premium_total=p.premium_total,
                trade_count=p.trade_count,
            )
            for p in periods
        ],
    )
