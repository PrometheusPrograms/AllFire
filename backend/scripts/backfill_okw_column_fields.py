"""Fill OKW column fields that were omitted on earlier imports.

Writes `delta`, `probability_of_winning` (Prob OTM), `final_arorc`, and
`result_net_credit` only when the DB value is still null. Match key is
account + ticker + type + open date + expiration + short strike
(+ contracts when both sides have them).

    python -m scripts.backfill_okw_column_fields \\
        --database-url ... --account rule1 --year 2026 --file /path/to/OKW_2026.xlsx
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal, ROUND_HALF_UP
from typing import Sequence

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Account, Trade
from scripts.import_historical import ACCOUNTS, _normalize_legacy_rut_spread_labels
from scripts.okw_parser import ParsedTrade, _prob_otm_from_delta, parse_trade_sheet
from scripts.xlsx_compat import load_workbook_safely

_CENTS = Decimal("0.01")


def _cents(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


def _match_key(parsed: ParsedTrade) -> tuple | None:
    if parsed.trade_date is None or parsed.ticker is None:
        return None
    return (
        parsed.ticker.upper(),
        parsed.trade_type_name,
        parsed.trade_date,
        parsed.expiration_date,
        _cents(parsed.short_strike),
        parsed.num_of_contracts,
    )


def apply_okw_column_fields(
    session: Session,
    parsed_trades: Sequence[ParsedTrade],
    *,
    account_name: str,
    dry_run: bool = False,
) -> list[tuple[int, Decimal | None, Decimal | None]]:
    """Return (trade_id, delta, probability_of_winning) for rows that change."""
    account = session.scalar(select(Account).where(Account.account_name == account_name))
    if account is None:
        raise ValueError(f"Account {account_name!r} not found")

    trades = session.scalars(select(Trade).where(Trade.account_id == account.id)).all()
    unused = list(trades)
    changed: list[tuple[int, Decimal | None, Decimal | None]] = []

    for parsed in parsed_trades:
        if (
            parsed.delta is None
            and parsed.probability_of_winning is None
            and parsed.final_arorc is None
            and parsed.result_net_credit is None
        ):
            continue
        key = _match_key(parsed)
        if key is None:
            continue
        ticker, trade_type, opened, expiration, strike, contracts = key
        candidates = [
            trade
            for trade in unused
            if trade.ticker.upper() == ticker
            and trade.trade_type == trade_type
            and trade.date_trade_open == opened
            and trade.expiration_date == expiration
            and _cents(trade.strike_price) == strike
            and (
                contracts is None
                or trade.num_of_contracts is None
                or trade.num_of_contracts == contracts
            )
        ]
        if not candidates:
            continue
        trade = candidates[0]
        unused.remove(trade)
        new_delta = parsed.delta if trade.delta is None else trade.delta
        if trade.probability_of_winning is None:
            new_pow = parsed.probability_of_winning or _prob_otm_from_delta(
                parsed.delta if parsed.delta is not None else trade.delta
            )
        else:
            new_pow = trade.probability_of_winning
        new_final = parsed.final_arorc if trade.final_arorc is None else trade.final_arorc
        new_net = (
            parsed.result_net_credit
            if trade.result_net_credit is None
            else trade.result_net_credit
        )
        if (
            new_delta == trade.delta
            and new_pow == trade.probability_of_winning
            and new_final == trade.final_arorc
            and new_net == trade.result_net_credit
        ):
            continue
        changed.append((trade.id, new_delta, new_pow))
        if not dry_run:
            trade.delta = new_delta
            trade.probability_of_winning = new_pow
            trade.final_arorc = new_final
            trade.result_net_credit = new_net
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--account", required=True, choices=sorted(ACCOUNTS))
    parser.add_argument("--year", required=True, type=int)
    parser.add_argument("--sheet-name", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    account_name, sheet_prefix = ACCOUNTS[args.account]
    sheet_name = args.sheet_name or f"{sheet_prefix} {args.year}"
    workbook = load_workbook_safely(args.file, data_only=True, read_only=False)
    if sheet_name not in workbook.sheetnames:
        print(f"Sheet {sheet_name!r} not found. Available: {workbook.sheetnames}", file=sys.stderr)
        return 1

    parsed = parse_trade_sheet(workbook[sheet_name])
    _normalize_legacy_rut_spread_labels(parsed)

    engine = create_engine(args.database_url)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        changed = apply_okw_column_fields(
            session, parsed, account_name=account_name, dry_run=args.dry_run
        )
        if args.dry_run:
            session.rollback()
        else:
            session.commit()

    prefix = "would update" if args.dry_run else "updated"
    print(f"{prefix} {len(changed)} trade(s) from {sheet_name!r}")
    for trade_id, delta, pow_ in changed[:40]:
        print(f"  trade_id={trade_id} delta={delta} probability_of_winning={pow_}")
    if len(changed) > 40:
        print(f"  ... {len(changed) - 40} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
