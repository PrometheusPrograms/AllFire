"""One-time backfill of `cost_basis` from existing `trades`/`trade_events`.

`cost_basis` (see docs/DATA_MODEL.md) is the only correct source for share
ownership and running basis, but `scripts/okw_loader.py` deliberately never
populates it on import (see its own docstring) — this script derives the
rows it would have written, from history that already exists:

- `BTO`/`STC`: one row per trade, straight from `num_of_shares`/
  `price_per_share`/`commission_per_share` on the trade itself.
- `ROCT PUT`/`RULE ONE PUT` with an `ASSIGN` event: one row — the short
  put was exercised, so `num_of_contracts * 100` shares were acquired at
  `strike_price`.
- `ROCT CALL`/`RULE ONE CALL` with an `ASSIGN` event: one row — shares were
  called away at `strike_price` (negative delta).
- `ROCS BULL PUT SPREAD` / `BULL PUT SPREAD`: never written to `cost_basis`.
  A spread cannot be assigned. When an equity (`ROCS`) spread is `CLOSE`d,
  the share-changing trade is a sibling `ROCT PUT` or `RULE ONE PUT` (same
  account/ticker/expiration/short strike, opened on the close date) — that
  PUT is what may later ASSIGN and create the cost-basis row. A closed
  `ROCS` spread with no such sibling PUT is flagged for review. An `ASSIGN`
  on either spread type is also flagged (shouldn't happen).
- Everything else (CLOSE/EXPIRE/ROLL, or an OPTIONS trade with no ASSIGN):
  no shares changed hands, contributes nothing.

Cost basis intentionally uses the raw strike price on assignment, not netted
against premium collected — premium is tracked/shown separately (see
`app.services.premium`) to avoid double-counting the same dollars two ways.

Idempotent on `cost_basis.trade_id`: a trade that already has a `cost_basis`
row is skipped, so re-running after new trades have been added (or after a
partial/interrupted run) never creates duplicates.

Usage:

    python -m scripts.backfill_cost_basis --database-url postgresql+psycopg://... [--dry-run]
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import create_engine

from app.models import CostBasis, Trade, TradeEvent, TradeType

ZERO = Decimal("0")
CONTRACT_MULTIPLIER = Decimal("100")

# Trade type names that acquire shares on ASSIGN (short put exercised).
# Spreads are intentionally absent — they cannot be assigned; a closed
# equity spread converts into one of these PUT types instead.
PUT_LIKE_ASSIGNABLE = {"ROCT PUT", "RULE ONE PUT"}
# Trade type names that give up shares on ASSIGN (covered call called away).
CALL_LIKE_ASSIGNABLE = {"ROCT CALL", "RULE ONE CALL"}
# Never feed cost_basis. Equity (ROCS) vs cash-settled index (RUT) are both
# excluded; only the equity variant is expected to have a sibling PUT on CLOSE.
SPREAD_TYPES = {"ROCS BULL PUT SPREAD", "BULL PUT SPREAD"}
EQUITY_SPREAD_TYPE = "ROCS BULL PUT SPREAD"
ASSOCIATED_PUT_TYPES = frozenset(PUT_LIKE_ASSIGNABLE)


@dataclass
class BackfillResult:
    rows_created: int = 0
    trades_skipped_existing: int = 0
    trades_skipped_no_event: int = 0
    flagged_spread_assignment: list[int] = field(default_factory=list)
    flagged_closed_spread_missing_put: list[int] = field(default_factory=list)


def _already_backfilled(session: Session, trade_id: int) -> bool:
    return (
        session.scalar(select(CostBasis.id).where(CostBasis.trade_id == trade_id)) is not None
    )


def _latest_event(session: Session, trade_id: int, event_type: str | None = None) -> TradeEvent | None:
    query = select(TradeEvent).where(TradeEvent.trade_id == trade_id)
    if event_type is not None:
        query = query.where(TradeEvent.event_type == event_type)
    return session.scalar(
        query.order_by(TradeEvent.event_date.desc(), TradeEvent.id.desc()).limit(1)
    )


def _latest_assign_event(session: Session, trade_id: int) -> TradeEvent | None:
    return _latest_event(session, trade_id, "ASSIGN")


def _associated_put_id(session: Session, spread: Trade, close_date) -> int | None:
    """Sibling ROCT/RULE ONE PUT opened when this equity spread was closed.

    Match is same account + ticker + expiration + short strike, opened on
    the CLOSE date. That's the conversion pattern in the historical books
    (close the long-put side of the spread, keep the short as a standalone
    PUT). `trade_parent_id` is unused here — import never populated it.
    """
    return session.scalar(
        select(Trade.id)
        .where(
            Trade.account_id == spread.account_id,
            Trade.ticker_id == spread.ticker_id,
            Trade.trade_type.in_(ASSOCIATED_PUT_TYPES),
            Trade.date_trade_open == close_date,
            Trade.expiration_date == spread.expiration_date,
            Trade.strike_price == spread.strike_price,
            Trade.id != spread.id,
        )
        .order_by(Trade.id)
        .limit(1)
    )


def backfill(session: Session, *, dry_run: bool = False) -> BackfillResult:
    result = BackfillResult()

    trades = session.execute(
        select(Trade, TradeType.type_name)
        .join(TradeType, TradeType.id == Trade.trade_type_id)
        .order_by(Trade.date_trade_open, Trade.id)
    ).all()

    for trade, type_name in trades:
        if _already_backfilled(session, trade.id):
            result.trades_skipped_existing += 1
            continue

        row: CostBasis | None = None

        if type_name == "BTO":
            shares = Decimal(trade.num_of_shares or 0)
            cost_per_share = trade.price_per_share or ZERO
            total_amount = shares * (cost_per_share + trade.commission_per_share)
            row = CostBasis(
                account_id=trade.account_id,
                ticker_id=trade.ticker_id,
                trade_id=trade.id,
                transaction_date=trade.date_trade_open,
                description=f"BTO {trade.ticker} {shares} sh @ {cost_per_share}",
                shares=int(shares),
                cost_per_share=cost_per_share,
                total_amount=total_amount,
            )
        elif type_name == "STC":
            shares = Decimal(trade.num_of_shares or 0)
            sale_price = trade.price_per_share or ZERO
            total_amount = -(shares * (sale_price - trade.commission_per_share))
            row = CostBasis(
                account_id=trade.account_id,
                ticker_id=trade.ticker_id,
                trade_id=trade.id,
                transaction_date=trade.date_trade_open,
                description=f"STC {trade.ticker} {shares} sh @ {sale_price}",
                shares=-int(shares),
                cost_per_share=sale_price,
                total_amount=total_amount,
            )
        elif type_name in SPREAD_TYPES:
            latest = _latest_event(session, trade.id)
            if latest is not None and latest.event_type == "ASSIGN":
                result.flagged_spread_assignment.append(trade.id)
            elif (
                type_name == EQUITY_SPREAD_TYPE
                and latest is not None
                and latest.event_type == "CLOSE"
                and _associated_put_id(session, trade, latest.event_date) is None
            ):
                result.flagged_closed_spread_missing_put.append(trade.id)
            result.trades_skipped_no_event += 1
        elif type_name in PUT_LIKE_ASSIGNABLE or type_name in CALL_LIKE_ASSIGNABLE:
            event = _latest_assign_event(session, trade.id)
            if event is None:
                result.trades_skipped_no_event += 1
            else:
                contracts = Decimal(trade.num_of_contracts or 0)
                strike = trade.strike_price or ZERO
                sign = 1 if type_name in PUT_LIKE_ASSIGNABLE else -1
                shares = sign * contracts * CONTRACT_MULTIPLIER
                total_amount = shares * strike
                verb = "Assigned (acquired)" if sign > 0 else "Assigned (called away)"
                row = CostBasis(
                    account_id=trade.account_id,
                    ticker_id=trade.ticker_id,
                    trade_id=trade.id,
                    transaction_date=event.event_date,
                    description=f"{verb}: {type_name} {trade.ticker} {contracts} contracts @ {strike}",
                    shares=int(shares),
                    cost_per_share=strike,
                    total_amount=total_amount,
                )
        else:
            result.trades_skipped_no_event += 1

        if row is not None:
            session.add(row)
            result.rows_created += 1

    if dry_run:
        session.rollback()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--database-url", required=True, help="Target DATABASE_URL.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Report only — rolls back, no DB writes kept."
    )
    args = parser.parse_args(argv)

    engine = create_engine(args.database_url)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as session:
        result = backfill(session, dry_run=args.dry_run)
        if not args.dry_run:
            session.commit()

    print(f"cost_basis rows created: {result.rows_created}")
    print(f"trades already backfilled (skipped): {result.trades_skipped_existing}")
    print(f"trades with nothing to backfill (no ASSIGN / not BTO-STC): {result.trades_skipped_no_event}")
    if result.flagged_spread_assignment:
        print(
            "FLAGGED — ASSIGN found on a bull put spread (spreads cannot be assigned), "
            f"not backfilled: trade ids {result.flagged_spread_assignment}"
        )
    if result.flagged_closed_spread_missing_put:
        print(
            "FLAGGED — closed ROCS BULL PUT SPREAD with no sibling ROCT PUT / "
            "RULE ONE PUT (same ticker/expiration/short strike, opened on the "
            f"close date): trade ids {result.flagged_closed_spread_missing_put}"
        )
    if args.dry_run:
        print("--dry-run set, transaction rolled back, no rows persisted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
