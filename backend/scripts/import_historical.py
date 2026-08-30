"""One-time historical import CLI — see docs/PRODUCTION_IMPORT_RUNBOOK.md.

Talks directly to a `DATABASE_URL` you supply, never through the running
app's API (staging or prod) — reuses the same parser/loader the staging
`POST /api/import/spreadsheet` endpoint uses, but as a script so production
imports never depend on that endpoint being deployed or reachable.

Usage (exactly per the runbook, run chronologically, one file per account
per year — `--account`/`--year` derive the sheet name and the import_batch
tag, matching the workbook's own "<ACCOUNT PREFIX> TRADES <YEAR>" naming):

    python -m scripts.import_historical \\
        --database-url postgresql+psycopg://... \\
        --account rule1 --year 2026 --file /path/to/OKW_2026.xlsx

    python -m scripts.import_historical \\
        --database-url postgresql+psycopg://... \\
        --account roth --year 2026 --file /path/to/OKW_2026.xlsx

Re-running the same (account, year) is refused unless --force is passed
(see docs/PRODUCTION_IMPORT_RUNBOOK.md §4 — idempotency).
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from scripts.okw_loader import ImportBatchExistsError, load_parsed_trades
from scripts.okw_parser import parse_trade_sheet
from scripts.xlsx_compat import load_workbook_safely

# --account CLI value -> (accounts.account_name, sheet name prefix)
ACCOUNTS = {
    "rule1": ("Rule 1", "TRADES"),
    "roth": ("Roth", "ROTH TRADES"),
}


def _normalize_legacy_rut_spread_labels(parsed_trades: list) -> None:
    """Older (2024/2025) workbooks label plain RUT-index bull put spreads
    inconsistently — sometimes `RUT BULL PUT SPREAD` (ticker `RUT`, correct
    already), sometimes `ED RUT BULL PUT SPREAD` (ticker misparsed as `ED`,
    an outdated type-name prefix, not a real ticker). Per user confirmation:
    normalize both to ticker `RUT` / type `BULL PUT SPREAD` — a distinct
    strategy from `ROCS BULL PUT SPREAD` (real-stock spreads), never merge
    the two.
    """
    for trade in parsed_trades:
        combined = f"{trade.ticker} {trade.trade_type_name}".upper()
        if "BULL PUT SPREAD" in combined and "ROCS" not in combined and "RUT" in combined:
            trade.ticker = "RUT"
            trade.trade_type_name = "BULL PUT SPREAD"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True, help="Target DATABASE_URL — never the running app's API.")
    parser.add_argument("--file", required=True, help="Path to the .xlsx workbook.")
    parser.add_argument("--account", required=True, choices=sorted(ACCOUNTS), help="Which account this file belongs to.")
    parser.add_argument("--year", required=True, type=int, help="Sheet/trade year, e.g. 2026.")
    parser.add_argument("--force", action="store_true", help="Wipe and re-insert if this batch was already imported.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and report only — no DB writes.")
    parser.add_argument(
        "--sheet-name",
        default=None,
        help=(
            "Override the default '<PREFIX> <YEAR>' sheet name lookup — older "
            "workbooks aren't consistently named (e.g. 'Rule1 TRADES 2024', "
            "'ROTH TRADES 2025 ' with a trailing space)."
        ),
    )
    args = parser.parse_args(argv)

    account_name, sheet_prefix = ACCOUNTS[args.account]
    sheet_name = args.sheet_name or f"{sheet_prefix} {args.year}"
    import_batch = f"{args.account}_{args.year}"

    workbook = load_workbook_safely(args.file, data_only=True, read_only=False)
    if sheet_name not in workbook.sheetnames:
        print(f"Sheet {sheet_name!r} not found. Available: {workbook.sheetnames}", file=sys.stderr)
        return 1

    parsed_trades = parse_trade_sheet(workbook[sheet_name])
    _normalize_legacy_rut_spread_labels(parsed_trades)
    print(f"Parsed {len(parsed_trades)} trade blocks from {sheet_name!r} in {args.file!r}.")

    review_count = sum(1 for t in parsed_trades if t.needs_review)
    if review_count:
        print(f"  {review_count} flagged needs_review — inspect before trusting downstream numbers:")
        for t in parsed_trades:
            if t.needs_review:
                print(f"    col {t.column_letter} ({t.ticker} {t.trade_type_name}): {t.warnings}")

    if args.dry_run:
        print("--dry-run set, no DB writes performed.")
        return 0

    engine = create_engine(args.database_url)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as session:
        try:
            result = load_parsed_trades(
                session,
                parsed_trades,
                account_name=account_name,
                import_batch=import_batch,
                force=args.force,
            )
        except ImportBatchExistsError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        session.commit()

    print(f"Inserted {result.trades_created} trades, {result.events_created} trade_events.")
    print(f"  needs_review: {result.needs_review}")
    if result.skipped_no_ticker:
        print(f"  skipped (no ticker): {result.skipped_no_ticker}")
    if result.unknown_trade_types:
        print(f"  skipped (unknown trade_type, not in trade_types table): {set(result.unknown_trade_types)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
