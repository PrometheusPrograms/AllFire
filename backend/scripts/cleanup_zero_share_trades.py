"""Delete BTO/STC trades that carry zero shares.

These are dividend-reinvestment (DRIP) fills and sub-one-share fractions sold
off, imported before `scripts.schwab_parser` sent them to review: a 0.19-share
buy rounded to 0 whole shares. They add nothing to share counts or basis but
show up as phantom rows in the ticker drawer. DRIP shares are represented only
by the consolidated whole-share catch-up BTOs (see `scripts/manual_entry.py`).

Writes a JSON backup of every deleted row (trade, events, cost_basis lots)
before deleting.

    python -m scripts.cleanup_zero_share_trades --database-url ... \\
        [--backup deleted_zero_share_trades.json] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import CostBasis, Trade, TradeEvent


def _as_dict(row) -> dict:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def cleanup_zero_share_trades(session: Session, *, dry_run: bool = False) -> list[dict]:
    """Return backup records for every zero-share BTO/STC (deleted unless dry_run)."""
    trades = session.scalars(
        select(Trade)
        .where(Trade.trade_type.in_(("BTO", "STC")), func.coalesce(Trade.num_of_shares, 0) == 0)
        .order_by(Trade.id)
    ).all()
    backup = []
    for trade in trades:
        backup.append(
            {
                "trade": _as_dict(trade),
                "trade_events": [
                    _as_dict(e)
                    for e in session.scalars(
                        select(TradeEvent).where(TradeEvent.trade_id == trade.id)
                    )
                ],
                "cost_basis": [
                    _as_dict(c)
                    for c in session.scalars(
                        select(CostBasis).where(CostBasis.trade_id == trade.id)
                    )
                ],
            }
        )
    if not dry_run and trades:
        ids = [trade.id for trade in trades]
        session.execute(delete(CostBasis).where(CostBasis.trade_id.in_(ids)))
        session.execute(delete(TradeEvent).where(TradeEvent.trade_id.in_(ids)))
        session.execute(delete(Trade).where(Trade.id.in_(ids)))
    return backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--backup", type=Path, default=Path("deleted_zero_share_trades.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    engine = create_engine(args.database_url)
    with sessionmaker(bind=engine)() as session:
        backup = cleanup_zero_share_trades(session, dry_run=args.dry_run)
        if args.dry_run:
            session.rollback()
        else:
            args.backup.write_text(json.dumps(backup, default=str, indent=1))
            session.commit()

    by_type: dict[str, int] = {}
    for record in backup:
        by_type[record["trade"]["trade_type"]] = by_type.get(record["trade"]["trade_type"], 0) + 1
    verb = "would delete" if args.dry_run else f"deleted (backup: {args.backup})"
    print(f"{verb} {len(backup)} zero-share trade(s): {by_type}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
