"""Ensure Rule 1 and Roth account rows exist (not seeded by Alembic).

    python -m scripts.seed_accounts --database-url ...
"""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Account

ACCOUNTS = [
    {
        "account_name": "Rule 1",
        "account_type": "INVESTMENT",
        "start_date": date(2013, 11, 6),
        "starting_balance": Decimal("0"),
        "is_default": True,
    },
    {
        "account_name": "Roth",
        "account_type": "RETIREMENT",
        "start_date": date(2024, 1, 1),
        "starting_balance": Decimal("0"),
        "is_default": False,
    },
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args(argv)

    engine = create_engine(args.database_url)
    factory = sessionmaker(bind=engine)
    created = 0
    with factory() as session:
        for spec in ACCOUNTS:
            existing = session.scalar(
                select(Account).where(Account.account_name == spec["account_name"])
            )
            if existing is not None:
                continue
            session.add(Account(**spec))
            created += 1
        session.commit()
    print(f"created {created} account(s); skipped {len(ACCOUNTS) - created} already present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
