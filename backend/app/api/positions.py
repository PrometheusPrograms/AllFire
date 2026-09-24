"""Ticker drill-down: `GET /api/positions/summary` and `GET /api/positions/ledger`.

Summary is lifetime shares / cost basis / premium for one ticker in one
account (no date filter). The ledger is the drawer table: every trade plus
cash dividends, tagged with a source (option / BTO / STC / dividend).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Account, CashFlow, CostBasis, Ticker, Trade, TradeEvent, TradeType
from app.services.position import PositionEntry, classify_share_class, summarize_positions
from app.services.premium import OPTIONS_CONTRACT_MULTIPLIER, premium_for_trade

router = APIRouter(prefix="/api/positions", tags=["positions"])

DbSession = Annotated[Session, Depends(get_db)]

LedgerSource = Literal["option", "BTO", "STC", "dividend"]
LedgerRowKind = Literal["trade", "dividend"]


class ClassTotalsOut(BaseModel):
    shares: Decimal
    cost_basis: Decimal
    avg_cost_per_share: Decimal | None


class PositionSummaryOut(BaseModel):
    ticker: str
    account_name: str
    total: ClassTotalsOut
    trading: ClassTotalsOut
    long_term: ClassTotalsOut
    total_premium_collected: Decimal


def _class_totals_out(totals) -> ClassTotalsOut:
    return ClassTotalsOut(
        shares=totals.shares,
        cost_basis=totals.cost_basis,
        avg_cost_per_share=totals.avg_cost_per_share,
    )


def _resolve_account_ticker(db: Session, account: str, ticker: str) -> tuple[int, int, str]:
    ticker = ticker.upper()
    account_row = db.execute(select(Account.id).where(Account.account_name == account)).first()
    if account_row is None:
        raise HTTPException(404, f"Account {account!r} not found.")
    ticker_row = db.execute(select(Ticker.id).where(Ticker.ticker == ticker)).first()
    if ticker_row is None:
        raise HTTPException(404, f"Ticker {ticker!r} not found.")
    return account_row[0], ticker_row[0], ticker


STOCK_TRADE_TYPES = {"BTO", "STC"}


def _ledger_strike_price(
    type_name: str, strike_price: Decimal | None, price_per_share: Decimal | None
) -> Decimal | None:
    """BTO/STC have no strike — the "Strike(s)" column shows the trade
    price instead, so a plain stock fill still reads as a dollar figure
    rather than a blank dash."""
    if type_name in STOCK_TRADE_TYPES:
        return price_per_share
    return strike_price


def _source_for_trade(type_name: str, category: str | None) -> LedgerSource:
    if type_name == "BTO":
        return "BTO"
    if type_name == "STC":
        return "STC"
    if category == "STOCK":
        return "BTO"
    return "option"


def _trade_share_count(num_of_shares: int | None, num_of_contracts: int | None) -> int | None:
    if num_of_shares is not None:
        return num_of_shares
    if num_of_contracts is not None:
        return int(Decimal(num_of_contracts) * OPTIONS_CONTRACT_MULTIPLIER)
    return None


def _trade_amount(
    *,
    category: str | None,
    is_credit: bool,
    net_credit_per_share: Decimal | None,
    price_per_share: Decimal | None,
    credit_debit: Decimal,
    num_of_contracts: int | None,
    num_of_shares: int | None,
) -> Decimal | None:
    premium = premium_for_trade(
        trade_type_category=category,
        is_credit=is_credit,
        net_credit_per_share=net_credit_per_share,
        num_of_contracts=num_of_contracts,
    )
    if premium is not None:
        return premium
    shares = _trade_share_count(num_of_shares, num_of_contracts)
    if shares is None:
        return credit_debit
    share_count = Decimal(shares)
    if price_per_share is not None:
        return price_per_share * share_count
    return credit_debit * share_count


_latest_event_type = (
    select(TradeEvent.event_type)
    .where(TradeEvent.trade_id == Trade.id)
    .order_by(TradeEvent.event_date.desc(), TradeEvent.id.desc())
    .limit(1)
    .correlate(Trade)
    .scalar_subquery()
)
_display_status_expr = case(
    (_latest_event_type == "CLOSE", "closed"),
    (_latest_event_type == "EXPIRE", "expired"),
    (_latest_event_type == "ASSIGN", "assigned"),
    (_latest_event_type == "ROLL", "rolled"),
    else_="open",
)


class LedgerRowOut(BaseModel):
    row_kind: LedgerRowKind
    source: LedgerSource
    id: int
    date: date
    trade_type: str
    strike_price: Decimal | None
    long_strike: Decimal | None
    expiration_date: date | None
    shares: int | None
    amount: Decimal | None
    # Plain stock trades (BTO/STC) have no lifecycle beyond the initial fill
    # — status ("Open"/"Assigned"/etc.) is an options concept and isn't
    # meaningful for them, so this is None rather than always "open".
    display_status: str | None


class PositionLedgerOut(BaseModel):
    ticker: str
    account_name: str
    items: list[LedgerRowOut]


@router.get("/summary", response_model=PositionSummaryOut)
def get_position_summary(
    db: DbSession,
    ticker: str,
    account: str,
) -> PositionSummaryOut:
    account_id, ticker_id, ticker = _resolve_account_ticker(db, account, ticker)

    cost_basis_rows = db.execute(
        select(CostBasis.shares, CostBasis.total_amount, TradeType.type_name)
        .select_from(CostBasis)
        .outerjoin(Trade, Trade.id == CostBasis.trade_id)
        .outerjoin(TradeType, TradeType.id == Trade.trade_type_id)
        .where(CostBasis.account_id == account_id, CostBasis.ticker_id == ticker_id)
    ).all()

    entries: list[PositionEntry] = [
        (classify_share_class(type_name), Decimal(shares), total_amount)
        for shares, total_amount, type_name in cost_basis_rows
    ]
    totals = summarize_positions(entries)

    trade_rows = db.execute(
        select(
            Trade.net_credit_per_share,
            Trade.num_of_contracts,
            TradeType.category,
            TradeType.is_credit,
        )
        .select_from(Trade)
        .join(TradeType, TradeType.id == Trade.trade_type_id)
        .where(Trade.account_id == account_id, Trade.ticker_id == ticker_id)
    ).all()

    total_premium_collected = sum(
        (
            premium
            for net_credit_per_share, num_of_contracts, category, is_credit in trade_rows
            if (
                premium := premium_for_trade(
                    trade_type_category=category,
                    is_credit=is_credit,
                    net_credit_per_share=net_credit_per_share,
                    num_of_contracts=num_of_contracts,
                )
            )
            is not None
        ),
        start=Decimal("0"),
    )

    return PositionSummaryOut(
        ticker=ticker,
        account_name=account,
        total=_class_totals_out(totals.total),
        trading=_class_totals_out(totals.trading),
        long_term=_class_totals_out(totals.long_term),
        total_premium_collected=total_premium_collected,
    )


@router.get("/ledger", response_model=PositionLedgerOut)
def get_position_ledger(
    db: DbSession,
    ticker: str,
    account: str,
) -> PositionLedgerOut:
    account_id, ticker_id, ticker = _resolve_account_ticker(db, account, ticker)

    trade_rows = db.execute(
        select(
            Trade.id,
            Trade.date_trade_open,
            Trade.trade_type,
            Trade.strike_price,
            Trade.long_strike,
            Trade.expiration_date,
            Trade.num_of_shares,
            Trade.num_of_contracts,
            Trade.net_credit_per_share,
            Trade.price_per_share,
            Trade.credit_debit,
            TradeType.category,
            TradeType.is_credit,
            _display_status_expr.label("display_status"),
        )
        .select_from(Trade)
        .join(TradeType, TradeType.id == Trade.trade_type_id)
        .where(Trade.account_id == account_id, Trade.ticker_id == ticker_id)
    ).all()

    items: list[LedgerRowOut] = []
    for row in trade_rows:
        items.append(
            LedgerRowOut(
                row_kind="trade",
                source=_source_for_trade(row.trade_type, row.category),
                id=row.id,
                date=row.date_trade_open,
                trade_type=row.trade_type,
                strike_price=_ledger_strike_price(
                    row.trade_type, row.strike_price, row.price_per_share
                ),
                long_strike=row.long_strike,
                expiration_date=row.expiration_date,
                shares=_trade_share_count(row.num_of_shares, row.num_of_contracts),
                amount=_trade_amount(
                    category=row.category,
                    is_credit=row.is_credit,
                    net_credit_per_share=row.net_credit_per_share,
                    price_per_share=row.price_per_share,
                    credit_debit=row.credit_debit,
                    num_of_contracts=row.num_of_contracts,
                    num_of_shares=row.num_of_shares,
                ),
                display_status=(
                    None if row.trade_type in STOCK_TRADE_TYPES else row.display_status
                ),
            )
        )

    dividend_rows = db.scalars(
        select(CashFlow).where(
            CashFlow.account_id == account_id,
            CashFlow.ticker_id == ticker_id,
            CashFlow.transaction_type == "DIVIDEND",
        )
    ).all()
    for flow in dividend_rows:
        items.append(
            LedgerRowOut(
                row_kind="dividend",
                source="dividend",
                id=flow.id,
                date=flow.transaction_date,
                trade_type="DIVIDEND",
                strike_price=None,
                long_strike=None,
                expiration_date=None,
                shares=None,
                amount=flow.amount,
                display_status="paid",
            )
        )

    items.sort(key=lambda row: (row.date, row.row_kind, row.id), reverse=True)
    return PositionLedgerOut(ticker=ticker, account_name=account, items=items)
