"""Replace the two LULU Schwab BTOs stored as 9 shares with 10-share lots.

Schwab's Trader API reports 9 shares; Rule 1 (742) statements show
Purchase 10.0000 at the same prices (2025-07-25 @ 218.99, 2025-07-30 @
214.99). Trades are immutable — delete the truncated rows and recreate
at statement quantity, keeping the original schwab_activity_id.

    python -m scripts.repair_lulu_truncated_btos --database-url ... [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Account, CostBasis, Ticker, Trade, TradeEvent
from scripts.schwab_loader import load_schwab
from scripts.schwab_parser import EquityBuy

TARGET_DATES = (date(2025, 7, 24), date(2025, 7, 29))
WRONG_SHARES = 9
RIGHT_SHARES = 10
TICKER = "LULU"
ACCOUNT = "Rule 1"
BATCH = "schwab_rule1_div_bto"
NOTE = (
    "Statement qty 10.0000 (742 2025-07-31); Schwab API reported 9. "
    "Recreated at statement quantity."
)


def find_truncated_lulu_btos(session: Session) -> list[Trade]:
    account = session.scalar(select(Account).where(Account.account_name == ACCOUNT))
    ticker = session.scalar(select(Ticker).where(Ticker.ticker == TICKER))
    if account is None or ticker is None:
        return []
    return list(
        session.scalars(
            select(Trade).where(
                Trade.account_id == account.id,
                Trade.ticker_id == ticker.id,
                Trade.trade_type == "BTO",
                Trade.num_of_shares == WRONG_SHARES,
                Trade.date_trade_open.in_(TARGET_DATES),
                Trade.import_batch.is_not(None),
                Trade.import_batch.startswith("schwab_"),
            )
        ).all()
    )


def _as_buys(trades: list[Trade]) -> list[EquityBuy]:
    buys: list[EquityBuy] = []
    for trade in trades:
        activity_id = trade.schwab_activity_id or f"lulu-stmt-qty-{trade.date_trade_open.isoformat()}"
        buys.append(
            EquityBuy(
                activity_id=activity_id,
                order_id=trade.schwab_order_id,
                transaction_date=trade.date_trade_open,
                ticker=TICKER,
                shares=RIGHT_SHARES,
                price=trade.price_per_share or Decimal("0"),
                fees=(trade.commission_per_share or Decimal("0")) * WRONG_SHARES,
                description=NOTE,
            )
        )
    return buys


def repair_truncated_lulu_btos(session: Session, *, dry_run: bool = False) -> list[int]:
    trades = find_truncated_lulu_btos(session)
    ids = [t.id for t in trades]
    buys = _as_buys(trades)
    if dry_run or not trades:
        return ids
    for trade in trades:
        session.execute(delete(CostBasis).where(CostBasis.trade_id == trade.id))
        session.execute(delete(TradeEvent).where(TradeEvent.trade_id == trade.id))
        session.execute(delete(Trade).where(Trade.id == trade.id))
    session.flush()
    load_schwab(
        session,
        account_name=ACCOUNT,
        import_batch=BATCH,
        dividends=[],
        equity_buys=buys,
        dry_run=False,
    )
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    engine = create_engine(args.database_url)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        ids = repair_truncated_lulu_btos(session, dry_run=args.dry_run)
        if args.dry_run:
            session.rollback()
        else:
            session.commit()

    prefix = "would replace" if args.dry_run else "replaced"
    print(f"{prefix} {len(ids)} LULU 9-share Schwab BTO(s) with 10-share statement lots: {ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
