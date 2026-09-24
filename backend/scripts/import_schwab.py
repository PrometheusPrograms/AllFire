"""Schwab gap-fill CLI — dividends → cash_flows, equity buys → BTO.

Talks to Schwab's Trader API and a DATABASE_URL you supply. Does not go
through the running app. Incremental: already-imported activity ids are
skipped. See docs/SCHWAB_IMPORT.md.

    python -m scripts.schwab_auth
    python -m scripts.import_schwab --database-url ... --account rule1 \\
        --start 2024-01-01 --end 2026-12-31 [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from scripts.okw_loader import get_or_create_account
from scripts.schwab_client import SchwabApiError, SchwabClient
from scripts.schwab_loader import load_schwab
from scripts.schwab_parser import classify_transactions
from scripts.schwab_tokens import DEFAULT_TOKEN_PATH, SchwabAuthError

ACCOUNTS = {
    "rule1": "Rule 1",
    "roth": "Roth",
}


def _account_hash(cli_account: str, override: str | None) -> str:
    if override:
        return override
    if cli_account == "rule1":
        stored = settings.schwab_account_hash_rule1
    else:
        stored = settings.schwab_account_hash_roth
    if stored:
        return stored
    raise SystemExit(
        "Pass --schwab-account-hash or set SCHWAB_ACCOUNT_HASH_"
        f"{cli_account.upper()} in backend/.env."
    )


def _print_result(result, *, dry_run: bool) -> None:
    prefix = "would " if dry_run else ""
    print(f"{prefix}create dividends: {result.dividends_created}")
    print(f"skipped dividends (already imported): {result.dividends_skipped}")
    print(f"{prefix}create BTO trades: {result.btos_created}")
    print(f"skipped BTO (already in DB): {result.btos_skipped_existing}")
    print(f"skipped BTO (matches ASSIGN lot): {result.btos_skipped_assign}")
    print(f"{prefix}create STC trades: {result.stcs_created}")
    print(f"skipped STC (already in DB): {result.stcs_skipped_existing}")
    print(f"skipped STC (matches ASSIGN lot): {result.stcs_skipped_assign}")
    print(f"review (not imported): {len(result.review)}")
    for item in result.review:
        desc = f" {item.description}" if item.description else ""
        print(f"  - {item.activity_id}: {item.reason}{desc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--database-url",
        required=True,
        help="Target DATABASE_URL — never the running app's API.",
    )
    parser.add_argument("--account", required=True, choices=sorted(ACCOUNTS))
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--schwab-account-hash", default=None)
    parser.add_argument("--tokens-file", default=str(DEFAULT_TOKEN_PATH))
    args = parser.parse_args(argv)

    if args.end < args.start:
        print("--end must be on or after --start", file=sys.stderr)
        return 1

    account_name = ACCOUNTS[args.account]
    import_batch = f"schwab_{args.account}_div_bto"
    tokens_path = Path(args.tokens_file)

    engine = create_engine(args.database_url)
    factory = sessionmaker(bind=engine)

    try:
        hash_value = _account_hash(args.account, args.schwab_account_hash)
        with SchwabClient(tokens_path=tokens_path) as client:
            transactions = client.fetch_transactions(hash_value, start=args.start, end=args.end)
    except (SchwabAuthError, SchwabApiError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    parsed = classify_transactions(transactions)

    with factory() as session:
        # Fail fast if the DB account row is missing (loader would raise too).
        get_or_create_account(session, account_name)
        result = load_schwab(
            session,
            account_name=account_name,
            import_batch=import_batch,
            dividends=parsed.dividends,
            equity_buys=parsed.equity_buys,
            equity_sells=parsed.equity_sells,
            review=parsed.review,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            session.rollback()
        else:
            session.commit()

    _print_result(result, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
