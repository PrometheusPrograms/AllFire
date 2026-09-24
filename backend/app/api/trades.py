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
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Account, Trade, TradeEvent, TradeType
from app.services.premium import premium_for_trade
from app.services.roll_chain import (
    build_chains,
    chain_arorc,
    classify_roll,
    format_strike_path,
    option_right,
)

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
# Plain stock fills (BTO/STC) have no option lifecycle — never "open".
_display_status_expr = sa.case(
    (TradeType.category == "STOCK", sa.null()),
    (_latest_event_type == "CLOSE", "closed"),
    (_latest_event_type == "EXPIRE", "expired"),
    (_latest_event_type == "ASSIGN", "assigned"),
    (_latest_event_type == "ROLL", "rolled"),
    else_="open",
).label("display_status")

_DISPLAY_STATUSES = ("open", "rolled", "assigned", "expired", "closed")

# OKW RESULT cell text, not the API's lowercase display_status.
_OKW_RESULT = {
    "open": "OPEN",
    "rolled": "ROLL",
    "assigned": "ASSIGNED",
    "expired": "EXPIRED",
    "closed": "CLOSED",
}

_RESULT_EVENT_TYPES = ("CLOSE", "EXPIRE", "ASSIGN", "ROLL")

_latest_result_closing_debit = (
    select(TradeEvent.closing_debit)
    .where(
        TradeEvent.trade_id == Trade.id,
        TradeEvent.event_type.in_(_RESULT_EVENT_TYPES),
    )
    .order_by(TradeEvent.event_date.desc(), TradeEvent.id.desc())
    .limit(1)
    .correlate(Trade)
    .scalar_subquery()
)
_latest_result_total_debit = (
    select(TradeEvent.total_debit)
    .where(
        TradeEvent.trade_id == Trade.id,
        TradeEvent.event_type.in_(_RESULT_EVENT_TYPES),
    )
    .order_by(TradeEvent.event_date.desc(), TradeEvent.id.desc())
    .limit(1)
    .correlate(Trade)
    .scalar_subquery()
)
_latest_result_date = (
    select(TradeEvent.event_date)
    .where(
        TradeEvent.trade_id == Trade.id,
        TradeEvent.event_type.in_(_RESULT_EVENT_TYPES),
    )
    .order_by(TradeEvent.event_date.desc(), TradeEvent.id.desc())
    .limit(1)
    .correlate(Trade)
    .scalar_subquery()
)


def _okw_result(display_status: str | None) -> str | None:
    if display_status is None:
        return None
    return _OKW_RESULT.get(display_status, display_status.upper())


def _result_net_credit(
    net_credit_per_share: Decimal | None,
    num_of_shares: int | None,
    total_debit: Decimal | None,
) -> Decimal | None:
    """OKW `NET CREDIT/(DEBIT)` — NC × shares, minus total debit when present."""
    if net_credit_per_share is None or num_of_shares is None:
        return net_credit_per_share
    total = net_credit_per_share * Decimal(num_of_shares)
    if total_debit is not None:
        total -= total_debit
    return total


class TradeEventOut(BaseModel):
    id: int
    event_type: str
    event_date: date
    closing_debit: Decimal | None
    total_debit: Decimal | None
    schwab_order_id: str | None
    notes: str | None


class ChainEventOut(TradeEventOut):
    trade_id: int
    strike_price: Decimal | None
    expiration_date: date | None
    date_trade_open: date | None = None
    net_credit_per_share: Decimal | None = None
    arorc: Decimal | None = None
    next_trade_id: int | None = None
    roll_kind: str | None = None
    option_right: str | None = None
    to_strike: Decimal | None = None
    to_expiration: date | None = None


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
    display_status: str | None
    date_trade_closed: date | None
    date_trade_rolled: date | None
    trade_parent_id: int | None = None
    roll_legs: int = 1
    strike_path: str | None = None
    chain_trade_ids: list[int] = Field(default_factory=list)
    premium_collected: Decimal | None = None
    chain_arorc: Decimal | None = None
    chain_net_credit_per_share: Decimal | None = None
    chain_dte: int | None = None
    closing_debit: Decimal | None = None
    total_debit: Decimal | None = None
    result: str | None = None
    result_date: date | None = None
    result_net_credit: Decimal | None = None
    delta: Decimal | None = None
    probability_of_winning: Decimal | None = None
    final_arorc: Decimal | None = None


class TradeDetailOut(TradeOut):
    events: list[TradeEventOut]
    running_basis: Decimal | None
    running_shares: Decimal | None
    chain_root_id: int
    chain_events: list[ChainEventOut]
    chain_legs: list[TradeOut]


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
    Trade.delta,
    Trade.probability_of_winning,
    Trade.final_arorc,
    Trade.result_net_credit,
    Trade.needs_review,
    _current_status.c.trade_status,
    _display_status_expr,
    _current_status.c.date_trade_closed,
    _current_status.c.date_trade_rolled,
    Trade.trade_parent_id,
    _latest_result_closing_debit.label("closing_debit"),
    _latest_result_total_debit.label("total_debit"),
    _latest_result_date.label("result_event_date"),
)


def _base_query():
    return (
        select(*_TRADE_COLUMNS)
        .select_from(Trade)
        .join(Account, Account.id == Trade.account_id)
        .outerjoin(TradeType, TradeType.id == Trade.trade_type_id)
        .join(_current_status, _current_status.c.trade_id == Trade.id)
    )


def _apply_identity_filters(
    query,
    *,
    account: str | None,
    ticker: str | None,
    trade_type: str | None,
    needs_review: bool | None,
):
    if account:
        query = query.where(Account.account_name == account)
    if ticker:
        query = query.where(Trade.ticker == ticker.upper())
    if trade_type:
        query = query.where(Trade.trade_type == trade_type)
    if needs_review is not None:
        query = query.where(Trade.needs_review == needs_review)
    return query


def _premium_for_row(row) -> Decimal | None:
    return premium_for_trade(
        trade_type_category=row["trade_type_category"],
        is_credit=row["is_credit"],
        net_credit_per_share=row["net_credit_per_share"],
        num_of_contracts=row["num_of_contracts"],
    )


def _trade_out_from_row(row, **overrides) -> TradeOut:
    payload = dict(row)
    payload.setdefault("chain_trade_ids", [row["id"]])
    payload.setdefault("roll_legs", 1)
    payload.setdefault("strike_path", None)
    payload.setdefault("premium_collected", _premium_for_row(row))
    payload.setdefault("chain_arorc", row["arorc"])
    payload.setdefault("chain_net_credit_per_share", row["net_credit_per_share"])
    payload.setdefault("chain_dte", row["days_to_expiration"])
    result = payload.get("result") or _okw_result(row["display_status"])
    result_date = payload.get("result_date")
    if result_date is None:
        result_date = row["result_event_date"]
    closing_debit = payload.get("closing_debit", row["closing_debit"])
    total_debit = payload.get("total_debit", row["total_debit"])
    payload.setdefault("result", result)
    payload.setdefault("result_date", result_date)
    payload.setdefault("closing_debit", closing_debit)
    payload.setdefault("total_debit", total_debit)
    if payload.get("result_net_credit") is None:
        payload["result_net_credit"] = _result_net_credit(
            row["net_credit_per_share"], row["num_of_shares"], total_debit
        )
    if payload.get("result") not in (None, "OPEN") and payload.get("closing_debit") is None:
        payload["closing_debit"] = Decimal("0")
    payload.update(overrides)
    return TradeOut(**payload)


def _collapse_chain(legs: list) -> TradeOut:
    root = legs[0]
    tip = legs[-1]
    premiums = [_premium_for_row(leg) for leg in legs]
    collected = None
    if any(p is not None for p in premiums):
        collected = sum((p for p in premiums if p is not None), start=Decimal("0"))
    credit = sum((leg["credit_debit"] for leg in legs), start=Decimal("0"))
    net_credits = [leg["net_credit_per_share"] for leg in legs]
    combined_nc = None
    if any(nc is not None for nc in net_credits):
        combined_nc = sum((nc for nc in net_credits if nc is not None), start=Decimal("0"))
    combined_dte = None
    if tip["expiration_date"] is not None:
        combined_dte = (tip["expiration_date"] - root["date_trade_open"]).days
    combined_arorc = chain_arorc(
        date_trade_open=root["date_trade_open"],
        expiration_date=tip["expiration_date"],
        net_credits=net_credits,
        risk_capital_per_share=root["risk_capital_per_share"],
        margin_percent=root["margin_percent"],
    )
    return _trade_out_from_row(
        root,
        expiration_date=tip["expiration_date"],
        days_to_expiration=combined_dte if combined_dte is not None else tip["days_to_expiration"],
        num_of_contracts=tip["num_of_contracts"],
        strike_price=tip["strike_price"],
        long_strike=tip["long_strike"],
        arorc=combined_arorc if combined_arorc is not None else tip["arorc"],
        trade_status=tip["trade_status"],
        display_status=tip["display_status"],
        date_trade_closed=tip["date_trade_closed"],
        date_trade_rolled=tip["date_trade_rolled"] or root["date_trade_rolled"],
        credit_debit=credit,
        net_credit_per_share=combined_nc,
        needs_review=any(leg["needs_review"] for leg in legs),
        trade_parent_id=None,
        roll_legs=len(legs),
        strike_path=format_strike_path([leg["strike_price"] for leg in legs]),
        chain_trade_ids=[leg["id"] for leg in legs],
        premium_collected=collected,
        chain_arorc=combined_arorc if combined_arorc is not None else tip["arorc"],
        chain_net_credit_per_share=combined_nc,
        chain_dte=combined_dte,
        result=_okw_result(tip["display_status"]),
        result_date=tip["result_event_date"],
        closing_debit=tip["closing_debit"],
        total_debit=tip["total_debit"],
        result_net_credit=_result_net_credit(
            combined_nc, tip["num_of_shares"], tip["total_debit"]
        ),
    )


def _chain_matches(
    legs: list,
    *,
    status: str | None,
    display_status: str | None,
    date_from: date | None,
    date_to: date | None,
    expiration_from: date | None,
    expiration_to: date | None,
    needs_review_computed: bool | None,
    as_of: date,
) -> bool:
    tip = legs[-1]
    if status and tip["trade_status"] != status:
        return False
    if display_status and tip["display_status"] != display_status:
        return False
    if date_from is not None or date_to is not None:
        in_range = False
        for leg in legs:
            opened = leg["date_trade_open"]
            if date_from is not None and opened < date_from:
                continue
            if date_to is not None and opened > date_to:
                continue
            in_range = True
            break
        if not in_range:
            return False
    exp = tip["expiration_date"]
    if expiration_from is not None and (exp is None or exp < expiration_from):
        return False
    if expiration_to is not None and (exp is None or exp > expiration_to):
        return False
    if needs_review_computed:
        if (
            tip["trade_status"] != "open"
            or exp is None
            or exp >= as_of
            or tip["trade_type_category"] == "STOCK"
        ):
            return False
    return True


def _walk_chain_ids(db: Session, trade_id: int) -> list[int]:
    parent_rows = db.execute(select(Trade.id, Trade.trade_parent_id)).all()
    parent_of = {row.id: row.trade_parent_id for row in parent_rows}
    if trade_id not in parent_of:
        return [trade_id]
    for chain in build_chains(parent_of):
        if trade_id in chain.ids:
            return list(chain.ids)
    return [trade_id]


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
    if status and status not in ("open", "closed"):
        raise HTTPException(400, "status must be 'open' or 'closed'")
    if display_status and display_status not in _DISPLAY_STATUSES:
        raise HTTPException(
            400, f"display_status must be one of {', '.join(_DISPLAY_STATUSES)}"
        )

    identity = _apply_identity_filters(
        _base_query(),
        account=account,
        ticker=ticker,
        trade_type=trade_type,
        needs_review=needs_review,
    )
    rows = db.execute(identity).mappings().all()
    by_id = {row["id"]: row for row in rows}
    parent_of = {row["id"]: row["trade_parent_id"] for row in rows}

    collapsed: list[TradeOut] = []
    for spec in build_chains(parent_of):
        legs = [by_id[leg_id] for leg_id in spec.ids if leg_id in by_id]
        if not legs:
            continue
        if not _chain_matches(
            legs,
            status=status,
            display_status=display_status,
            date_from=date_from,
            date_to=date_to,
            expiration_from=expiration_from,
            expiration_to=expiration_to,
            needs_review_computed=needs_review_computed,
            as_of=resolved_as_of,
        ):
            continue
        collapsed.append(_collapse_chain(legs))

    collapsed.sort(key=lambda item: (item.date_trade_open, item.id), reverse=True)
    total = len(collapsed)
    page = collapsed[offset : offset + limit]
    return TradeListOut(total=total, items=page)


@router.get("/{trade_id}", response_model=TradeDetailOut)
def get_trade(trade_id: int, db: Session = Depends(get_db)) -> TradeDetailOut:
    chain_ids = _walk_chain_ids(db, trade_id)
    rows = {
        row["id"]: row
        for row in db.execute(_base_query().where(Trade.id.in_(chain_ids))).mappings().all()
    }
    focused = rows.get(trade_id)
    if focused is None:
        raise HTTPException(404, f"Trade {trade_id} not found")

    ordered_rows = [rows[i] for i in chain_ids if i in rows]
    chain_legs = [_trade_out_from_row(row) for row in ordered_rows]

    events_by_trade: dict[int, list] = {i: [] for i in chain_ids}
    event_rows = db.execute(
        select(TradeEvent)
        .where(TradeEvent.trade_id.in_(chain_ids))
        .order_by(TradeEvent.event_date, TradeEvent.id)
    ).scalars().all()
    for event in event_rows:
        events_by_trade.setdefault(event.trade_id, []).append(event)

    chain_events: list[ChainEventOut] = []
    for index, row in enumerate(ordered_rows):
        nxt = ordered_rows[index + 1] if index + 1 < len(ordered_rows) else None
        next_id = nxt["id"] if nxt is not None else None
        right = option_right(row["trade_type"])
        for event in events_by_trade.get(row["id"], []):
            kind = None
            to_strike = None
            to_expiration = None
            if event.event_type == "ROLL" and nxt is not None:
                kind = classify_roll(
                    from_strike=row["strike_price"],
                    from_expiration=row["expiration_date"],
                    to_strike=nxt["strike_price"],
                    to_expiration=nxt["expiration_date"],
                )
                to_strike = nxt["strike_price"]
                to_expiration = nxt["expiration_date"]
            chain_events.append(
                ChainEventOut(
                    id=event.id,
                    event_type=event.event_type,
                    event_date=event.event_date,
                    closing_debit=event.closing_debit,
                    total_debit=event.total_debit,
                    schwab_order_id=event.schwab_order_id,
                    notes=event.notes,
                    trade_id=row["id"],
                    strike_price=row["strike_price"],
                    expiration_date=row["expiration_date"],
                    date_trade_open=row["date_trade_open"],
                    net_credit_per_share=row["net_credit_per_share"],
                    arorc=row["arorc"],
                    next_trade_id=next_id if event.event_type == "ROLL" else None,
                    roll_kind=kind,
                    option_right=right,
                    to_strike=to_strike,
                    to_expiration=to_expiration,
                )
            )

    focused_events = events_by_trade.get(trade_id, [])
    cost_basis_row = db.execute(
        select(_cost_basis_running.c.running_basis, _cost_basis_running.c.running_shares)
        .where(_cost_basis_running.c.trade_id == trade_id)
        .order_by(
            _cost_basis_running.c.transaction_date.desc(), _cost_basis_running.c.id.desc()
        )
        .limit(1)
    ).first()

    collapsed = _collapse_chain(ordered_rows) if len(ordered_rows) > 1 else _trade_out_from_row(focused)
    return TradeDetailOut(
        **collapsed.model_dump(),
        events=[TradeEventOut.model_validate(e, from_attributes=True) for e in focused_events],
        running_basis=cost_basis_row.running_basis if cost_basis_row else None,
        running_shares=cost_basis_row.running_shares if cost_basis_row else None,
        chain_root_id=chain_ids[0],
        chain_events=chain_events,
        chain_legs=chain_legs,
    )
