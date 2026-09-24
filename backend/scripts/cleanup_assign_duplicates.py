"""Delete Schwab BTO/STC rows that duplicate an OKW ASSIGN lot.

Repairs databases imported before settlement-lag matching existed in
`scripts.schwab_loader`. Read the list with --dry-run first.

    python -m scripts.cleanup_assign_duplicates --database-url ... [--dry-run]
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from scripts.schwab_loader import delete_schwab_rows_matching_assign


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
        deleted = delete_schwab_rows_matching_assign(session, dry_run=args.dry_run)
        if args.dry_run:
            session.rollback()
        else:
            session.commit()

    prefix = "would delete" if args.dry_run else "deleted"
    print(f"{prefix} {len(deleted)} Schwab BTO/STC trade(s) matching ASSIGN lots")
    for trade_id in deleted:
        print(f"  trade_id={trade_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
