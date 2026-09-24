"""Load classified Schwab rows into cash_flows / BTO trades / cost_basis.

Incremental: idempotent on schwab_activity_id (and schwab_order_id when
present). Does not wipe an import_batch. Skips equity buys that already
exist as BTO or as an OKW-derived ASSIGN cost-basis lot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import CashFlow, CostBasis, Trade, TradeEvent
from scripts.okw_loader import get_or_create_account, get_or_create_ticker, get_trade_type
from scripts.schwab_parser import CashDividend, EquityBuy, EquitySell, ReviewItem

ZERO = Decimal("0")
PRICE_TOLERANCE = Decimal("0.01")
BTO_TYPE_NAME = "BTO"
STC_TYPE_NAME = "STC"
# Schwab posts assignment equity legs on settlement, often several days after
# the OKW ASSIGN event_date. Match within this window at the strike price.
ASSIGN_SETTLEMENT_DAYS = 45
PUT_LIKE_ASSIGNABLE = {"ROCT PUT", "RULE ONE PUT"}
CALL_LIKE_ASSIGNABLE = {"ROCT CALL", "RULE ONE CALL"}
STRIKE_RELATIVE_TOLERANCE = Decimal("0.05")
PUT_LIKE_ASSIGNABLE = {"ROCT PUT", "RULE ONE PUT"}
CALL_LIKE_ASSIGNABLE = {"ROCT CALL", "RULE ONE CALL"}


@dataclass
class SchwabLoadResult:
    dividends_created: int = 0
    dividends_skipped: int = 0
    btos_created: int = 0
    btos_skipped_existing: int = 0
    btos_skipped_assign: int = 0
    stcs_created: int = 0
    stcs_skipped_existing: int = 0
    stcs_skipped_assign: int = 0
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


def _matching_stc(
    session: Session,
    *,
    account_id: int,
    ticker_id: int,
    txn_date: date,
    shares: int,
    price: Decimal,
) -> Trade | None:
    candidates = session.scalars(
        select(Trade).where(
            Trade.account_id == account_id,
            Trade.ticker_id == ticker_id,
            Trade.trade_type == STC_TYPE_NAME,
            Trade.date_trade_open == txn_date,
            Trade.num_of_shares == shares,
        )
    ).all()
    for trade in candidates:
        if _prices_similar(trade.price_per_share, price):
            return trade
    return None


def _prices_similar(left: Decimal | None, right: Decimal | None) -> bool:
    if left is None or right is None:
        return True
    return abs(left - right) <= PRICE_TOLERANCE


def _strike_similar(price: Decimal | None, strike: Decimal | None) -> bool:
    """Assignment fills are at strike; Schwab sometimes prints a nearby market price."""
    if price is None or strike is None:
        return False
    if abs(price - strike) <= PRICE_TOLERANCE:
        return True
    scale = max(abs(strike), Decimal("1"))
    return (abs(price - strike) / scale) <= STRIKE_RELATIVE_TOLERANCE


def _assign_candidates(
    session: Session,
    *,
    account_id: int,
    ticker_id: int,
    txn_date: date,
    side: Literal["buy", "sell"],
) -> list[tuple[Trade, TradeEvent]]:
    types = PUT_LIKE_ASSIGNABLE if side == "buy" else CALL_LIKE_ASSIGNABLE
    window_start = txn_date - timedelta(days=ASSIGN_SETTLEMENT_DAYS)
    return list(
        session.execute(
            select(Trade, TradeEvent)
            .join(TradeEvent, TradeEvent.trade_id == Trade.id)
            .where(
                Trade.account_id == account_id,
                Trade.ticker_id == ticker_id,
                Trade.trade_type.in_(types),
                TradeEvent.event_type == "ASSIGN",
                TradeEvent.event_date >= window_start,
                TradeEvent.event_date <= txn_date,
            )
        ).all()
    )


def _matches_assign_lot(
    session: Session,
    *,
    account_id: int,
    ticker_id: int,
    txn_date: date,
    shares: int,
    price: Decimal,
    side: Literal["buy", "sell"],
) -> bool:
    """True when this equity BUY/SELL is Schwab's settlement of an OKW ASSIGN.

    PUT assignment acquires shares (BTO duplicate). CALL assignment delivers
    shares (STC duplicate). Schwab often dates the equity leg on settlement,
    up to ASSIGN_SETTLEMENT_DAYS after the option event, at (or near) the strike.
    A single Schwab fill can cover several same-strike assignment lots.
    """
    matching_shares: list[int] = []
    for trade, _event in _assign_candidates(
        session,
        account_id=account_id,
        ticker_id=ticker_id,
        txn_date=txn_date,
        side=side,
    ):
        assigned_shares = (trade.num_of_contracts or 0) * 100
        if assigned_shares <= 0:
            continue
        if assigned_shares == shares and _strike_similar(price, trade.strike_price):
            return True
        if _prices_similar(price, trade.strike_price):
            matching_shares.append(assigned_shares)
    if not matching_shares:
        return False
    assigned_total = sum(matching_shares)
    if assigned_total == shares:
        return True
    # Schwab sometimes bundles the assignment share delivery with a leftover
    # odd lot (typically one contract / 100 shares) into a single equity SELL.
    extra = shares - assigned_total
    return extra > 0 and extra <= 100


def delete_schwab_rows_matching_assign(
    session: Session,
    *,
    dry_run: bool = False,
) -> list[int]:
    """Delete Schwab BTO/STC trades that duplicate an OKW ASSIGN cost-basis lot.

    Used to repair imports that ran before settlement-lag matching existed.
    """
    schwab_trades = session.scalars(
        select(Trade).where(
            Trade.trade_type.in_((BTO_TYPE_NAME, STC_TYPE_NAME)),
            Trade.import_batch.is_not(None),
            Trade.import_batch.startswith("schwab_"),
        )
    ).all()
    deleted_ids: list[int] = []
    for trade in schwab_trades:
        if trade.num_of_shares is None or trade.price_per_share is None:
            continue
        side: Literal["buy", "sell"] = "buy" if trade.trade_type == BTO_TYPE_NAME else "sell"
        if not _matches_assign_lot(
            session,
            account_id=trade.account_id,
            ticker_id=trade.ticker_id,
            txn_date=trade.date_trade_open,
            shares=trade.num_of_shares,
            price=trade.price_per_share,
            side=side,
        ):
            continue
        deleted_ids.append(trade.id)
        if dry_run:
            continue
        session.execute(delete(CostBasis).where(CostBasis.trade_id == trade.id))
        session.execute(delete(TradeEvent).where(TradeEvent.trade_id == trade.id))
        session.execute(delete(Trade).where(Trade.id == trade.id))
    return deleted_ids


def load_schwab(
    session: Session,
    *,
    account_name: str,
    import_batch: str,
    dividends: list[CashDividend],
    equity_buys: list[EquityBuy],
    equity_sells: list[EquitySell] | None = None,
    review: list[ReviewItem] | None = None,
    dry_run: bool = False,
) -> SchwabLoadResult:
    account = get_or_create_account(session, account_name)
    bto_type = get_trade_type(session, BTO_TYPE_NAME)
    if bto_type is None:
        raise ValueError("Trade type 'BTO' is not seeded — run alembic upgrades first.")
    stc_type = get_trade_type(session, STC_TYPE_NAME)
    if stc_type is None:
        raise ValueError("Trade type 'STC' is not seeded — run alembic upgrades first.")

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
        if _matches_assign_lot(
            session,
            account_id=account.id,
            ticker_id=ticker.id,
            txn_date=buy.transaction_date,
            shares=buy.shares,
            price=buy.price,
            side="buy",
        ):
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

    for sell in equity_sells or []:
        if _existing_trade_by_activity(session, sell.activity_id) is not None:
            result.stcs_skipped_existing += 1
            continue
        if sell.order_id and _existing_trade_by_order(session, sell.order_id) is not None:
            result.stcs_skipped_existing += 1
            continue
        ticker = get_or_create_ticker(session, sell.ticker)
        if (
            _matching_stc(
                session,
                account_id=account.id,
                ticker_id=ticker.id,
                txn_date=sell.transaction_date,
                shares=sell.shares,
                price=sell.price,
            )
            is not None
        ):
            result.stcs_skipped_existing += 1
            continue
        if _matches_assign_lot(
            session,
            account_id=account.id,
            ticker_id=ticker.id,
            txn_date=sell.transaction_date,
            shares=sell.shares,
            price=sell.price,
            side="sell",
        ):
            result.stcs_skipped_assign += 1
            continue

        shares = Decimal(sell.shares)
        commission_per_share = (sell.fees / shares) if shares != ZERO else ZERO
        total_amount = -(shares * (sell.price - commission_per_share))
        result.stcs_created += 1
        if dry_run:
            continue

        trade = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker=ticker.ticker,
            trade_type_id=stc_type.id,
            trade_type=STC_TYPE_NAME,
            date_trade_open=sell.transaction_date,
            num_of_shares=sell.shares,
            price_per_share=sell.price,
            credit_debit=sell.price,
            commission_per_share=commission_per_share,
            total_amount=total_amount,
            schwab_order_id=sell.order_id or sell.activity_id,
            schwab_activity_id=sell.activity_id,
            notes=sell.description,
            import_batch=import_batch,
        )
        session.add(trade)
        session.flush()
        session.add(
            TradeEvent(
                trade_id=trade.id,
                event_type="OPEN",
                event_date=sell.transaction_date,
                schwab_order_id=sell.order_id,
                import_batch=import_batch,
            )
        )
        session.add(
            CostBasis(
                account_id=account.id,
                ticker_id=ticker.id,
                trade_id=trade.id,
                transaction_date=sell.transaction_date,
                description=f"STC {ticker.ticker} {shares} sh @ {sell.price}",
                shares=-sell.shares,
                cost_per_share=sell.price,
                total_amount=total_amount,
            )
        )

    return result
