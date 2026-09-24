"""Load hand-typed STC / BTO / dividend rows (JSON or CSV).

`scripts/import_schwab.py` now imports equity BUY, equity SELL, and cash
dividends directly from Schwab. This CLI remains for genuine gaps Schwab's
API can't classify automatically — interest, DRIP, assignment/exercise
legs, pre-Schwab-window history, or hand adjustments — printed to its
`review` list. Writes through the same tables (`trades` + OPEN +
`cost_basis`, or `cash_flows`) with `import_batch` tagging so
`validate_import.py` can see them.

Default batch is `manual`. Idempotent: matching existing BTO/STC (account,
ticker, date, shares, similar price) or dividend (account, ticker, date,
amount) is skipped. Optional `schwab_activity_id` uses the unique keys.

    python -m scripts.manual_entry --database-url ... --file rows.json [--dry-run]

JSON is a list of objects. CSV uses the same column names.

    {"kind": "stc", "account": "Rule 1", "ticker": "ADBE", "date": "2025-06-01",
     "shares": 100, "price": "12.50", "commission_per_share": "0.00"}
    {"kind": "bto", ...same fields...}
    {"kind": "dividend", "account": "Rule 1", "ticker": "AAPL", "date": "2025-03-15",
     "amount": "12.34", "description": "optional"}
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import CashFlow, CostBasis, Trade, TradeEvent
from scripts.okw_loader import get_or_create_account, get_or_create_ticker, get_trade_type
from scripts.schwab_loader import _matching_bto, _matching_stc
from scripts.schwab_parser import EquityBuy

ZERO = Decimal("0")
DEFAULT_BATCH = "manual"
STOCK_KINDS = {"stc", "bto"}


@dataclass
class ManualLoadResult:
    created: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return ZERO
    return Decimal(str(value))


def _as_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _as_int(value: Any) -> int:
    return int(value)


def parse_records_file(path: Path) -> list[dict[str, Any]]:
    text = path.read_text()
    if path.suffix.lower() == ".json":
        payload = json.loads(text)
        if not isinstance(payload, list):
            raise ValueError("JSON file must be a list of row objects.")
        return payload
    reader = csv.DictReader(text.splitlines())
    return [dict(row) for row in reader]


def _existing_dividend(
    session: Session,
    *,
    account_id: int,
    ticker_id: int | None,
    txn_date: date,
    amount: Decimal,
    activity_id: str | None,
) -> CashFlow | None:
    if activity_id:
        found = session.scalar(select(CashFlow).where(CashFlow.schwab_activity_id == activity_id))
        if found is not None:
            return found
    query = select(CashFlow).where(
        CashFlow.account_id == account_id,
        CashFlow.transaction_date == txn_date,
        CashFlow.transaction_type == "DIVIDEND",
        CashFlow.amount == amount,
    )
    if ticker_id is not None:
        query = query.where(CashFlow.ticker_id == ticker_id)
    return session.scalar(query)


def load_manual_records(
    session: Session,
    records: list[dict[str, Any]],
    *,
    import_batch: str = DEFAULT_BATCH,
    dry_run: bool = False,
) -> ManualLoadResult:
    result = ManualLoadResult()
    for index, raw in enumerate(records):
        kind = str(raw.get("kind") or "").strip().lower()
        label = f"row {index + 1}"
        try:
            if kind == "dividend":
                _load_dividend(
                    session, raw, import_batch=import_batch, dry_run=dry_run, result=result
                )
            elif kind in STOCK_KINDS:
                _load_stock(
                    session,
                    raw,
                    kind=kind.upper(),
                    import_batch=import_batch,
                    dry_run=dry_run,
                    result=result,
                )
            else:
                result.errors.append(f"{label}: unknown kind {kind!r} (use bto, stc, dividend)")
        except (KeyError, TypeError, ValueError) as exc:
            result.errors.append(f"{label}: {exc}")
    return result


def _load_dividend(
    session: Session,
    raw: dict[str, Any],
    *,
    import_batch: str,
    dry_run: bool,
    result: ManualLoadResult,
) -> None:
    account = get_or_create_account(session, str(raw["account"]))
    ticker = get_or_create_ticker(session, str(raw["ticker"])) if raw.get("ticker") else None
    txn_date = _as_date(raw["date"])
    amount = _dec(raw["amount"])
    activity_id = (raw.get("schwab_activity_id") or None) or None
    if _existing_dividend(
        session,
        account_id=account.id,
        ticker_id=ticker.id if ticker is not None else None,
        txn_date=txn_date,
        amount=amount,
        activity_id=activity_id,
    ):
        result.skipped += 1
        return
    result.created += 1
    if dry_run:
        return
    session.add(
        CashFlow(
            account_id=account.id,
            ticker_id=ticker.id if ticker is not None else None,
            trade_id=None,
            transaction_date=txn_date,
            transaction_type="DIVIDEND",
            amount=amount,
            description=raw.get("description") or raw.get("notes"),
            schwab_activity_id=activity_id,
            import_batch=raw.get("import_batch") or import_batch,
        )
    )


def _load_stock(
    session: Session,
    raw: dict[str, Any],
    *,
    kind: str,
    import_batch: str,
    dry_run: bool,
    result: ManualLoadResult,
) -> None:
    account = get_or_create_account(session, str(raw["account"]))
    ticker = get_or_create_ticker(session, str(raw["ticker"]))
    txn_date = _as_date(raw["date"])
    shares = _as_int(raw["shares"])
    price = _dec(raw["price"])
    commission = _dec(raw.get("commission_per_share"))
    activity_id = raw.get("schwab_activity_id") or None
    if activity_id:
        existing = session.scalar(select(Trade).where(Trade.schwab_activity_id == activity_id))
        if existing is not None:
            result.skipped += 1
            return
    if kind == "BTO":
        buy = EquityBuy(
            activity_id=activity_id or "",
            order_id=None,
            transaction_date=txn_date,
            ticker=ticker.ticker,
            shares=shares,
            price=price,
            fees=commission * Decimal(shares),
            description=str(raw.get("notes") or ""),
        )
        if _matching_bto(session, account_id=account.id, ticker_id=ticker.id, buy=buy):
            result.skipped += 1
            return
    else:
        if _matching_stc(
            session,
            account_id=account.id,
            ticker_id=ticker.id,
            txn_date=txn_date,
            shares=shares,
            price=price,
        ):
            result.skipped += 1
            return

    trade_type = get_trade_type(session, kind)
    if trade_type is None:
        raise ValueError(f"Trade type {kind!r} is not seeded — run alembic upgrades first.")

    share_qty = Decimal(shares)
    if kind == "BTO":
        total_amount = share_qty * (price + commission)
        basis_shares = shares
        basis_total = total_amount
    else:
        total_amount = -(share_qty * (price - commission))
        basis_shares = -shares
        basis_total = total_amount

    result.created += 1
    if dry_run:
        return

    batch = raw.get("import_batch") or import_batch
    trade = Trade(
        account_id=account.id,
        ticker_id=ticker.id,
        ticker=ticker.ticker,
        trade_type_id=trade_type.id,
        trade_type=kind,
        date_trade_open=txn_date,
        num_of_shares=shares,
        price_per_share=price,
        credit_debit=price,
        commission_per_share=commission,
        total_amount=total_amount,
        schwab_activity_id=activity_id,
        notes=raw.get("notes"),
        import_batch=batch,
    )
    session.add(trade)
    session.flush()
    session.add(
        TradeEvent(
            trade_id=trade.id,
            event_type="OPEN",
            event_date=txn_date,
            import_batch=batch,
        )
    )
    session.add(
        CostBasis(
            account_id=account.id,
            ticker_id=ticker.id,
            trade_id=trade.id,
            transaction_date=txn_date,
            description=f"{kind} {ticker.ticker} {shares} sh @ {price}",
            shares=basis_shares,
            cost_per_share=price,
            total_amount=basis_total,
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--file", required=True, help="JSON list or CSV of manual rows.")
    parser.add_argument("--import-batch", default=DEFAULT_BATCH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    path = Path(args.file)
    try:
        records = parse_records_file(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Could not read {path}: {exc}", file=sys.stderr)
        return 1

    engine = create_engine(args.database_url)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        result = load_manual_records(
            session,
            records,
            import_batch=args.import_batch,
            dry_run=args.dry_run,
        )
        if args.dry_run or result.errors:
            session.rollback()
        else:
            session.commit()

    prefix = "would " if args.dry_run else ""
    print(f"{prefix}create: {result.created}")
    print(f"skipped (already present): {result.skipped}")
    if result.errors:
        print("errors:")
        for err in result.errors:
            print(f"  {err}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
