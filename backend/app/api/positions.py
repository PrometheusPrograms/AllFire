"""`GET /api/positions/summary` — the ticker drill-down's backend: lifetime
shares owned (trading vs. long-term), cost basis, and total premium
collected for one ticker within one account. Deliberately not subject to
any date range — see the ticker drill-down plan.

The trade list itself isn't duplicated here; the frontend gets that from
the existing `GET /api/trades?ticker=&account=` (already unbounded by date
when no `date_from`/`date_to` is passed).
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Account, CostBasis, Ticker, Trade, TradeType
from app.services.position import PositionEntry, classify_share_class, summarize_positions
from app.services.premium import premium_for_trade

router = APIRouter(prefix="/api/positions", tags=["positions"])


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


@router.get("/summary", response_model=PositionSummaryOut)
def get_position_summary(
    ticker: str = Query(..., description="Ticker symbol, e.g. ADBE."),
    account: str = Query(..., description='Account name, e.g. "Rule 1".'),
    db: Session = Depends(get_db),
) -> PositionSummaryOut:
    ticker = ticker.upper()

    account_row = db.execute(
        select(Account.id).where(Account.account_name == account)
    ).first()
    if account_row is None:
        raise HTTPException(404, f"Account {account!r} not found.")
    account_id = account_row[0]

    ticker_row = db.execute(select(Ticker.id).where(Ticker.ticker == ticker)).first()
    if ticker_row is None:
        raise HTTPException(404, f"Ticker {ticker!r} not found.")
    ticker_id = ticker_row[0]

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
