"""One-shot repair: move ASSIGN events stamped on the open date to the expiration.

Older OKW loads stamped closing events as `result_date or trade_date`, and
RESULT DATE is usually blank for assignments, so the ASSIGN landed on the day
the option was *sold*. That's not just a cosmetic date: the cost_basis lot
backfilled from it inherits the wrong acquisition date, and
`scripts.schwab_loader`'s ASSIGN matching (a 45-day settlement window before
the Schwab equity fill) then misses the lot — so Schwab's share delivery was
imported as a separate BTO/STC and the shares were counted twice (GDXY, Rule 1).

Only the loader-bug signature is rewritten: event_date == date_trade_open with
a later expiration_date. An ASSIGN carrying any other date may be a genuine
early assignment and is left alone (same caution as
`backfill_expire_event_dates`). The trade's cost_basis lot dated on the old
day is moved with it.

    python -m scripts.backfill_assign_event_dates --database-url ... [--dry-run]
"""

from __future__ import annotations

import argparse

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import CostBasis, Trade, TradeEvent


def backfill_assign_dates(
    session: Session, *, dry_run: bool = False
) -> list[tuple[int, object, object]]:
    """Return (event_id, old_date, new_date) for ASSIGN rows that would change."""
    rows = session.execute(
        select(TradeEvent, Trade)
        .join(Trade, Trade.id == TradeEvent.trade_id)
        .where(TradeEvent.event_type == "ASSIGN")
    ).all()
    changed: list[tuple[int, object, object]] = []
    for event, trade in rows:
        if trade.expiration_date is None:
            continue
        if event.event_date != trade.date_trade_open:
            continue
        if trade.expiration_date <= trade.date_trade_open:
            continue
        old_date, new_date = event.event_date, trade.expiration_date
        changed.append((event.id, old_date, new_date))
        if dry_run:
            continue
        event.event_date = new_date
        for lot in session.scalars(
            select(CostBasis).where(
                CostBasis.trade_id == trade.id, CostBasis.transaction_date == old_date
            )
        ):
            lot.transaction_date = new_date
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
        changed = backfill_assign_dates(session, dry_run=args.dry_run)
        if args.dry_run:
            session.rollback()
        else:
            session.commit()

    prefix = "would update" if args.dry_run else "updated"
    print(f"{prefix} {len(changed)} ASSIGN event(s) (and their cost_basis lot dates)")
    for event_id, old, new in changed[:50]:
        print(f"  event_id={event_id} {old} -> {new}")
    if len(changed) > 50:
        print(f"  ... {len(changed) - 50} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
