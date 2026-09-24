"""Replace GOOG/GOOGL 5+95 split BTOs with one restated original lot.

The Jul 2022 20:1 split was modeled as a zero-cost 95-share BTO on the
split date. Those shares were acquired with the original 2014-02-25 lot;
restating keeps acquisition date, share count (100), and total basis.

    python -m scripts.repair_641_split_lots --database-url ... [--dry-run]
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Account, CostBasis, Ticker, Trade, TradeEvent
from scripts.manual_entry import load_manual_records

ACCOUNT = "Rule 1"
BATCH = "acct641_migration"
ORIGIN = date(2014, 2, 25)
SPLIT = date(2022, 7, 18)
TICKERS = ("GOOG", "GOOGL")
JSON_PATH = Path(__file__).with_name("data") / "acct641_manual_entries.json"


def _account_ticker(session: Session, ticker: str) -> tuple[int, int] | None:
    account = session.scalar(select(Account).where(Account.account_name == ACCOUNT))
    row = session.scalar(select(Ticker).where(Ticker.ticker == ticker))
    if account is None or row is None:
        return None
    return account.id, row.id


def find_split_modeled_trades(session: Session) -> list[Trade]:
    found: list[Trade] = []
    for ticker in TICKERS:
        ids = _account_ticker(session, ticker)
        if ids is None:
            continue
        account_id, ticker_id = ids
        found.extend(
            session.scalars(
                select(Trade).where(
                    Trade.account_id == account_id,
                    Trade.ticker_id == ticker_id,
                    Trade.trade_type == "BTO",
                    Trade.import_batch == BATCH,
                    Trade.date_trade_open.in_((ORIGIN, SPLIT)),
                )
            ).all()
        )
    return found


def _replacement_records() -> list[dict]:
    raw = json.loads(JSON_PATH.read_text())
    return [
        row
        for row in raw
        if row.get("kind") == "bto"
        and row.get("ticker") in TICKERS
        and row.get("date") == ORIGIN.isoformat()
        and int(row.get("shares") or 0) == 100
    ]


def repair_split_lots(session: Session, *, dry_run: bool = False) -> list[int]:
    trades = find_split_modeled_trades(session)
    ids = [t.id for t in trades]
    replacements = _replacement_records()
    if dry_run or not trades:
        return ids
    session.execute(delete(CostBasis).where(CostBasis.trade_id.in_(ids)))
    session.execute(delete(TradeEvent).where(TradeEvent.trade_id.in_(ids)))
    session.execute(delete(Trade).where(Trade.id.in_(ids)))
    session.flush()
    load_manual_records(session, replacements, import_batch=BATCH)
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
        ids = repair_split_lots(session, dry_run=args.dry_run)
        if not args.dry_run:
            session.commit()
    prefix = "dry-run: would replace" if args.dry_run else "replaced"
    print(f"{prefix} {len(ids)} GOOG/GOOGL split-modeled BTO(s): {ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
