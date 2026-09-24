"""One-shot repair: set EXPIRE trade_events.event_date to the contract expiration.

The OKW loader used to stamp EXPIRE as `result_date or trade_date`. RESULT DATE
is often blank, so OPEN and EXPIRE shared the trade date even when
`trades.expiration_date` was correct. New imports use expiration_date; this
rewrites already-loaded EXPIRE rows.

Not a domain-model edit of history — it corrects a loader bug. ROLL/CLOSE/ASSIGN
dates are left alone (those may carry a real result_date).

    python -m scripts.backfill_expire_event_dates --database-url ... [--dry-run]
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Trade, TradeEvent


def backfill_expire_dates(session: Session, *, dry_run: bool = False) -> list[tuple[int, object, object]]:
    """Return (event_id, old_date, new_date) for rows that would change."""
    rows = session.execute(
        select(TradeEvent, Trade)
        .join(Trade, Trade.id == TradeEvent.trade_id)
        .where(TradeEvent.event_type == "EXPIRE")
    ).all()
    changed: list[tuple[int, object, object]] = []
    for event, trade in rows:
        if trade.expiration_date is None:
            continue
        if event.event_date == trade.expiration_date:
            continue
        changed.append((event.id, event.event_date, trade.expiration_date))
        if not dry_run:
            event.event_date = trade.expiration_date
    return changed


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
        changed = backfill_expire_dates(session, dry_run=args.dry_run)
        if args.dry_run:
            session.rollback()
        else:
            session.commit()

    prefix = "would update" if args.dry_run else "updated"
    print(f"{prefix} {len(changed)} EXPIRE event(s)")
    for event_id, old, new in changed[:50]:
        print(f"  event_id={event_id} {old} -> {new}")
    if len(changed) > 50:
        print(f"  ... {len(changed) - 50} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
