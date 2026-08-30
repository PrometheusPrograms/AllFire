"""Read-only trade routes: `GET /api/trades` (filterable/paginated list) and
`GET /api/trades/{id}` (single trade + full event timeline).

Status (`open`/`closed`) is never stored — always joined in live from
`v_trade_current_status`, per the append-only design in docs/DATA_MODEL.md.
Write operations (create, roll, assign, expire, close) are a separate,
not-yet-built set of routes — see the plan's `add-trade-form` and
`lifecycle-actions` items.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Account, Trade, TradeEvent, TradeType

router = APIRouter(prefix="/api/trades", tags=["trades"])

# Views have no ORM model (see docs/DATA_MODEL.md — Alembic manages them as
# raw SQL); these are thin, read-only column handles for querying them.
_current_status = sa.table(
    "v_trade_current_status",
    sa.column("trade_id"),
    sa.column("trade_status"),
    sa.column("date_trade_closed"),
    sa.column("closing_debit"),
    sa.column("date_trade_rolled"),
)
_cost_basis_running = sa.table(
    "v_cost_basis_running",
    sa.column("trade_id"),
    sa.column("running_basis"),
    sa.column("running_shares"),
    sa.column("transaction_date"),
    sa.column("id"),
)

# `display_status` distinguishes rolled/assigned/expired/closed (the
# `status` param above only tracks the coarse open/closed split from the
# view). Derived from each trade's most recent trade_events row rather than
# a new column/view, since it's purely a display convenience and doesn't
# need to be queried outside this API.
_latest_event_type = (
    select(TradeEvent.event_type)
    .where(TradeEvent.trade_id == Trade.id)
    .order_by(TradeEvent.event_date.desc(), TradeEvent.id.desc())
    .limit(1)
    .correlate(Trade)
    .scalar_subquery()
)
_display_status_expr = sa.case(
    (_latest_event_type == "CLOSE", "closed"),
    (_latest_event_type == "EXPIRE", "expired"),
    (_latest_event_type == "ASSIGN", "assigned"),
    (_latest_event_type == "ROLL", "rolled"),
    else_="open",
).label("display_status")

_DISPLAY_STATUSES = ("open", "rolled", "assigned", "expired", "closed")


class TradeEventOut(BaseModel):
    id: int
    event_type: str
    event_date: date
    closing_debit: Decimal | None
    total_debit: Decimal | None
    schwab_order_id: str | None
    notes: str | None


class TradeOut(BaseModel):
    id: int
    account_id: int
    account_name: str
    ticker: str
    trade_type: str
    trade_type_category: str | None
    is_credit: bool
    date_trade_open: date
    expiration_date: date | None
    days_to_expiration: int | None
    num_of_contracts: int | None
    num_of_shares: int | None
    strike_price: Decimal | None
    long_strike: Decimal | None
    price_per_share: Decimal | None
    credit_debit: Decimal
    commission_per_share: Decimal
    net_credit_per_share: Decimal | None
    risk_capital_per_share: Decimal | None
    margin_percent: Decimal | None
    arorc: Decimal | None
    needs_review: bool
    trade_status: str
    display_status: str
    date_trade_closed: date | None
    date_trade_rolled: date | None


class TradeDetailOut(TradeOut):
    events: list[TradeEventOut]
    running_basis: Decimal | None
    running_shares: Decimal | None


class TradeListOut(BaseModel):
    total: int
    items: list[TradeOut]


_TRADE_COLUMNS = (
    Trade.id,
    Trade.account_id,
    Account.account_name,
    Trade.ticker,
    Trade.trade_type,
    TradeType.category.label("trade_type_category"),
    func.coalesce(TradeType.is_credit, False).label("is_credit"),
    Trade.date_trade_open,
    Trade.expiration_date,
    Trade.days_to_expiration,
    Trade.num_of_contracts,
    Trade.num_of_shares,
    Trade.strike_price,
    Trade.long_strike,
    Trade.price_per_share,
    Trade.credit_debit,
    Trade.commission_per_share,
    Trade.net_credit_per_share,
    Trade.risk_capital_per_share,
    Trade.margin_percent,
    Trade.arorc,
    Trade.needs_review,
    _current_status.c.trade_status,
    _display_status_expr,
    _current_status.c.date_trade_closed,
    _current_status.c.date_trade_rolled,
)


def _base_query():
    return (
        select(*_TRADE_COLUMNS)
        .select_from(Trade)
        .join(Account, Account.id == Trade.account_id)
        .outerjoin(TradeType, TradeType.id == Trade.trade_type_id)
        .join(_current_status, _current_status.c.trade_id == Trade.id)
    )


def _apply_filters(
    query,
    *,
    account: str | None,
    ticker: str | None,
    trade_type: str | None,
    status: str | None,
    display_status: str | None,
    needs_review: bool | None,
    needs_review_computed: bool | None,
    as_of: date,
    date_from: date | None,
    date_to: date | None,
    expiration_from: date | None,
    expiration_to: date | None,
):
    if account:
        query = query.where(Account.account_name == account)
    if ticker:
        query = query.where(Trade.ticker == ticker.upper())
    if trade_type:
        query = query.where(Trade.trade_type == trade_type)
    if status:
        if status not in ("open", "closed"):
            raise HTTPException(400, "status must be 'open' or 'closed'")
        query = query.where(_current_status.c.trade_status == status)
    if display_status:
        if display_status not in _DISPLAY_STATUSES:
            raise HTTPException(
                400, f"display_status must be one of {', '.join(_DISPLAY_STATUSES)}"
            )
        query = query.where(_display_status_expr == display_status)
    if needs_review is not None:
        query = query.where(Trade.needs_review == needs_review)
    if needs_review_computed:
        # Mirrors app.api.analytics's needs_review_count: still open per the
        # event log, but the expiration date has already passed — a data
        # quality signal (a CLOSE/EXPIRE/ASSIGN event is probably missing).
        query = query.where(
            _current_status.c.trade_status == "open",
            Trade.expiration_date.is_not(None),
            Trade.expiration_date < as_of,
        )
    if date_from:
        query = query.where(Trade.date_trade_open >= date_from)
    if date_to:
        query = query.where(Trade.date_trade_open <= date_to)
    if expiration_from:
        query = query.where(Trade.expiration_date >= expiration_from)
    if expiration_to:
        query = query.where(Trade.expiration_date <= expiration_to)
    return query


@router.get("", response_model=TradeListOut)
def list_trades(
    account: str | None = Query(None, description='Account name, e.g. "Rule 1".'),
    ticker: str | None = Query(None, description="Ticker symbol, e.g. SLV."),
    trade_type: str | None = Query(None, description='e.g. "ROCT PUT".'),
    status: str | None = Query(None, description="'open' or 'closed'."),
    display_status: str | None = Query(
        None, description="One of: open, rolled, assigned, expired, closed."
    ),
    needs_review: bool | None = Query(None),
    needs_review_computed: bool | None = Query(
        None, description="Open trades whose expiration date has already passed."
    ),
    as_of: date | None = Query(None, description="Defaults to today; used by needs_review_computed."),
    date_from: date | None = Query(None, description="Filter on date_trade_open >= this."),
    date_to: date | None = Query(None, description="Filter on date_trade_open <= this."),
    expiration_from: date | None = Query(None, description="Filter on expiration_date >= this."),
    expiration_to: date | None = Query(None, description="Filter on expiration_date <= this."),
    limit: int = Query(100, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> TradeListOut:
    resolved_as_of = as_of or date.today()
    filtered = _apply_filters(
        _base_query(),
        account=account,
        ticker=ticker,
        trade_type=trade_type,
        status=status,
        display_status=display_status,
        needs_review=needs_review,
        needs_review_computed=needs_review_computed,
        as_of=resolved_as_of,
        date_from=date_from,
        date_to=date_to,
        expiration_from=expiration_from,
        expiration_to=expiration_to,
    )

    count_query = _apply_filters(
        select(func.count(Trade.id))
        .select_from(Trade)
        .join(Account, Account.id == Trade.account_id)
        .join(_current_status, _current_status.c.trade_id == Trade.id),
        account=account,
        ticker=ticker,
        trade_type=trade_type,
        status=status,
        display_status=display_status,
        needs_review=needs_review,
        needs_review_computed=needs_review_computed,
        as_of=resolved_as_of,
        date_from=date_from,
        date_to=date_to,
        expiration_from=expiration_from,
        expiration_to=expiration_to,
    )
    total = db.scalar(count_query) or 0

    rows = db.execute(
        filtered.order_by(Trade.date_trade_open.desc(), Trade.id.desc())
        .limit(limit)
        .offset(offset)
    ).mappings().all()

    return TradeListOut(total=total, items=[TradeOut(**row) for row in rows])


@router.get("/{trade_id}", response_model=TradeDetailOut)
def get_trade(trade_id: int, db: Session = Depends(get_db)) -> TradeDetailOut:
    row = db.execute(_base_query().where(Trade.id == trade_id)).mappings().first()
    if row is None:
        raise HTTPException(404, f"Trade {trade_id} not found")

    events = db.execute(
        select(TradeEvent)
        .where(TradeEvent.trade_id == trade_id)
        .order_by(TradeEvent.event_date, TradeEvent.id)
    ).scalars().all()

    cost_basis_row = db.execute(
        select(_cost_basis_running.c.running_basis, _cost_basis_running.c.running_shares)
        .where(_cost_basis_running.c.trade_id == trade_id)
        .order_by(
            _cost_basis_running.c.transaction_date.desc(), _cost_basis_running.c.id.desc()
        )
        .limit(1)
    ).first()

    return TradeDetailOut(
        **row,
        events=[TradeEventOut.model_validate(e, from_attributes=True) for e in events],
        running_basis=cost_basis_row.running_basis if cost_basis_row else None,
        running_shares=cost_basis_row.running_shares if cost_basis_row else None,
    )
