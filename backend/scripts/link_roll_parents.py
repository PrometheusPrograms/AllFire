"""Set `trades.trade_parent_id` so OKW roll columns form a chain.

Each rolled column stays its own `trades` row; the next column (same
account/ticker/type/contracts, opened on the ROLL date, closest strike)
points at the previous via `trade_parent_id`. Safe to re-run: already-linked
children are left alone.

    python -m scripts.link_roll_parents --database-url "$DATABASE_URL" [--dry-run]
"""

from __future__ import annotations

import argparse

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import create_engine

from app.models import Trade, TradeEvent, TradeType
from app.services.roll_chain import RollLinkRow, match_roll_parents


def _latest_event_subq():
    return (
        select(TradeEvent.event_type)
        .where(TradeEvent.trade_id == Trade.id)
        .order_by(TradeEvent.event_date.desc(), TradeEvent.id.desc())
        .limit(1)
        .correlate(Trade)
        .scalar_subquery()
    )


def _roll_date_subq():
    return (
        select(TradeEvent.event_date)
        .where(TradeEvent.trade_id == Trade.id, TradeEvent.event_type == "ROLL")
        .order_by(TradeEvent.event_date.desc(), TradeEvent.id.desc())
        .limit(1)
        .correlate(Trade)
        .scalar_subquery()
    )


def load_link_rows(session: Session, account_id: int | None = None) -> list[RollLinkRow]:
    latest = _latest_event_subq()
    roll_date = _roll_date_subq()
    query = (
        select(
            Trade.id,
            Trade.account_id,
            Trade.ticker,
            Trade.trade_type,
            Trade.num_of_contracts,
            Trade.date_trade_open,
            Trade.expiration_date,
            Trade.strike_price,
            Trade.trade_parent_id,
            latest.label("latest_event"),
            roll_date.label("roll_date"),
        )
        .select_from(Trade)
        .join(TradeType, TradeType.id == Trade.trade_type_id)
        .where(TradeType.category != "STOCK")
    )
    if account_id is not None:
        query = query.where(Trade.account_id == account_id)
    rows = session.execute(query).all()
    return [
        RollLinkRow(
            id=row.id,
            account_id=row.account_id,
            ticker=row.ticker,
            trade_type=row.trade_type,
            num_of_contracts=row.num_of_contracts,
            date_trade_open=row.date_trade_open,
            expiration_date=row.expiration_date,
            strike_price=row.strike_price,
            trade_parent_id=row.trade_parent_id,
            latest_event=row.latest_event,
            roll_date=row.roll_date,
        )
        for row in rows
    ]


def apply_roll_parent_links(
    session: Session, *, account_id: int | None = None, dry_run: bool = False
) -> list[tuple[int, int]]:
    links = match_roll_parents(load_link_rows(session, account_id))
    if dry_run:
        return links
    for child_id, parent_id in links:
        child = session.get(Trade, child_id)
        if child is not None:
            child.trade_parent_id = parent_id
    return links


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
        links = apply_roll_parent_links(session, dry_run=args.dry_run)
        if args.dry_run:
            session.rollback()
        else:
            session.commit()

    prefix = "would link" if args.dry_run else "linked"
    print(f"{prefix} {len(links)} roll continuation(s)")
    for child_id, parent_id in links[:40]:
        print(f"  trade {child_id} <- parent {parent_id}")
    if len(links) > 40:
        print(f"  ... {len(links) - 40} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
