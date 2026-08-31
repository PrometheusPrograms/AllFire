"""Load classified Schwab rows into cash_flows / BTO trades / cost_basis.

Incremental: idempotent on schwab_activity_id (and schwab_order_id when
present). Does not wipe an import_batch. Skips equity buys that already
exist as BTO or as an OKW-derived ASSIGN cost-basis lot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CashFlow, CostBasis, Trade, TradeEvent
from scripts.okw_loader import get_or_create_account, get_or_create_ticker, get_trade_type
from scripts.schwab_parser import CashDividend, EquityBuy, ReviewItem

ZERO = Decimal("0")
PRICE_TOLERANCE = Decimal("0.01")
BTO_TYPE_NAME = "BTO"


@dataclass
class SchwabLoadResult:
    dividends_created: int = 0
    dividends_skipped: int = 0
    btos_created: int = 0
    btos_skipped_existing: int = 0
    btos_skipped_assign: int = 0
    review: list[ReviewItem] = field(default_factory=list)


def _prices_similar(left: Decimal | None, right: Decimal | None) -> bool:
    if left is None or right is None:
        return True
    return abs(left - right) <= PRICE_TOLERANCE


def _existing_cash_flow(session: Session, activity_id: str) -> CashFlow | None:
    return session.scalar(select(CashFlow).where(CashFlow.schwab_activity_id == activity_id))


def _existing_trade_by_activity(session: Session, activity_id: str) -> Trade | None:
    return session.scalar(select(Trade).where(Trade.schwab_activity_id == activity_id))


def _existing_trade_by_order(session: Session, order_id: str) -> Trade | None:
    return session.scalar(select(Trade).where(Trade.schwab_order_id == order_id))


def _matching_bto(
    session: Session,
    *,
    account_id: int,
    ticker_id: int,
    buy: EquityBuy,
) -> Trade | None:
    candidates = session.scalars(
        select(Trade).where(
            Trade.account_id == account_id,
            Trade.ticker_id == ticker_id,
            Trade.trade_type == BTO_TYPE_NAME,
            Trade.date_trade_open == buy.transaction_date,
            Trade.num_of_shares == buy.shares,
        )
    ).all()
    for trade in candidates:
        if _prices_similar(trade.price_per_share, buy.price):
            return trade
    return None


def _matches_assign_lot(
    session: Session,
    *,
    account_id: int,
    ticker_id: int,
    buy: EquityBuy,
) -> bool:
    rows = session.scalars(
        select(CostBasis).where(
            CostBasis.account_id == account_id,
            CostBasis.ticker_id == ticker_id,
            CostBasis.transaction_date == buy.transaction_date,
            CostBasis.shares == buy.shares,
        )
    ).all()
    for row in rows:
        if row.trade_id is None:
            continue
        assigned = session.scalar(
            select(TradeEvent.id).where(
                TradeEvent.trade_id == row.trade_id,
                TradeEvent.event_type == "ASSIGN",
            )
        )
        if assigned is not None:
            return True

    assign_events = session.execute(
        select(Trade, TradeEvent)
        .join(TradeEvent, TradeEvent.trade_id == Trade.id)
        .where(
            Trade.account_id == account_id,
            Trade.ticker_id == ticker_id,
            TradeEvent.event_type == "ASSIGN",
            TradeEvent.event_date == buy.transaction_date,
        )
    ).all()
    for trade, _event in assign_events:
        assigned_shares = (trade.num_of_contracts or 0) * 100
        if assigned_shares == buy.shares:
            return True
    return False


def load_schwab(
    session: Session,
    *,
    account_name: str,
    import_batch: str,
    dividends: list[CashDividend],
    equity_buys: list[EquityBuy],
    review: list[ReviewItem] | None = None,
    dry_run: bool = False,
) -> SchwabLoadResult:
    account = get_or_create_account(session, account_name)
    bto_type = get_trade_type(session, BTO_TYPE_NAME)
    if bto_type is None:
        raise ValueError("Trade type 'BTO' is not seeded — run alembic upgrades first.")

    result = SchwabLoadResult(review=list(review or []))

    for dividend in dividends:
        if _existing_cash_flow(session, dividend.activity_id) is not None:
            result.dividends_skipped += 1
            continue
        ticker = get_or_create_ticker(session, dividend.ticker) if dividend.ticker else None
        result.dividends_created += 1
        if dry_run:
            continue
        session.add(
            CashFlow(
                account_id=account.id,
                ticker_id=ticker.id if ticker is not None else None,
                trade_id=None,
                transaction_date=dividend.transaction_date,
                transaction_type="DIVIDEND",
                amount=dividend.amount,
                description=dividend.description,
                schwab_activity_id=dividend.activity_id,
                import_batch=import_batch,
            )
        )

    for buy in equity_buys:
        if _existing_trade_by_activity(session, buy.activity_id) is not None:
            result.btos_skipped_existing += 1
            continue
        if buy.order_id and _existing_trade_by_order(session, buy.order_id) is not None:
            result.btos_skipped_existing += 1
            continue
        ticker = get_or_create_ticker(session, buy.ticker)
        if _matching_bto(session, account_id=account.id, ticker_id=ticker.id, buy=buy) is not None:
            result.btos_skipped_existing += 1
            continue
        if _matches_assign_lot(session, account_id=account.id, ticker_id=ticker.id, buy=buy):
            result.btos_skipped_assign += 1
            continue

        shares = Decimal(buy.shares)
        commission_per_share = (buy.fees / shares) if shares != ZERO else ZERO
        total_amount = shares * (buy.price + commission_per_share)
        result.btos_created += 1
        if dry_run:
            continue

        trade = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker=ticker.ticker,
            trade_type_id=bto_type.id,
            trade_type=BTO_TYPE_NAME,
            date_trade_open=buy.transaction_date,
            num_of_shares=buy.shares,
            price_per_share=buy.price,
            credit_debit=buy.price,
            commission_per_share=commission_per_share,
            total_amount=total_amount,
            schwab_order_id=buy.order_id or buy.activity_id,
            schwab_activity_id=buy.activity_id,
            notes=buy.description,
            import_batch=import_batch,
        )
        session.add(trade)
        session.flush()
        session.add(
            TradeEvent(
                trade_id=trade.id,
                event_type="OPEN",
                event_date=buy.transaction_date,
                schwab_order_id=buy.order_id,
                import_batch=import_batch,
            )
        )
        session.add(
            CostBasis(
                account_id=account.id,
                ticker_id=ticker.id,
                trade_id=trade.id,
                transaction_date=buy.transaction_date,
                description=f"BTO {ticker.ticker} {shares} sh @ {buy.price}",
                shares=buy.shares,
                cost_per_share=buy.price,
                total_amount=total_amount,
            )
        )

    return result
