"""Throwaway: load the 6 short-call round trips that happened inside account
641 (NVDA + TSLA), reconstructed from the statements. All were sold-to-open
and either expired worthless or were bought-to-close -- none were assigned,
so they never touched the transferred share positions. Included per user
request, correlated to Rule 1 (the account these are being merged into).

Run with --dry-run first.
"""

import argparse
import sys
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Trade, TradeEvent
from scripts.okw_loader import get_or_create_account, get_or_create_ticker, get_trade_type

BATCH = "acct641_migration"
ACCOUNT = "Rule 1"

# (ticker, open_date, expiration, strike, contracts, open_price, commission_total,
#  close_kind[EXPIRE/CLOSE], close_date, close_price)
OPTIONS = [
    ("NVDA", date(2024, 6, 25), date(2024, 7, 5), Decimal("152.00"), 2, Decimal("0.19"), Decimal("1.01"),
     "EXPIRE", date(2024, 7, 8), None),
    ("TSLA", date(2025, 3, 13), date(2025, 3, 14), Decimal("265.00"), 2, Decimal("2.56"), Decimal("0.94"),
     "EXPIRE", date(2025, 3, 17), None),
    ("TSLA", date(2025, 3, 24), date(2025, 3, 28), Decimal("270.00"), 2, Decimal("0.92"), Decimal("0.94"),
     "EXPIRE", date(2025, 3, 31), None),
    ("TSLA", date(2025, 7, 10), date(2025, 7, 18), Decimal("322.50"), 1, Decimal("1.71"), Decimal("0.46"),
     "CLOSE", date(2025, 7, 21), Decimal("5.95")),
    ("TSLA", date(2025, 7, 21), date(2025, 8, 1), Decimal("355.00"), 1, Decimal("6.53"), Decimal("0.46"),
     "EXPIRE", date(2025, 8, 4), None),
    ("TSLA", date(2025, 8, 11), date(2025, 8, 15), Decimal("355.00"), 1, Decimal("1.40"), Decimal("0.46"),
     "EXPIRE", date(2025, 8, 18), None),
]

NOTE = (
    "Acct 641->Rule 1 reconstruction: short call opened and closed/expired "
    "entirely within account 641, before its shares were transferred into "
    "Rule 1 in Aug 2025. Never assigned -- no impact on share count/cost basis."
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    engine = create_engine(args.database_url)
    factory = sessionmaker(bind=engine)
    created = 0
    with factory() as session:
        account = get_or_create_account(session, ACCOUNT)
        trade_type = get_trade_type(session, "ROCT CALL")
        if trade_type is None:
            print("ROCT CALL trade type not seeded", file=sys.stderr)
            return 1

        for (
            symbol,
            open_date,
            expiration,
            strike,
            contracts,
            open_price,
            commission_total,
            close_kind,
            close_date,
            close_price,
        ) in OPTIONS:
            ticker = get_or_create_ticker(session, symbol)
            commission_per_share = commission_total / (Decimal(contracts) * 100)
            total_premium = open_price * Decimal(contracts) * 100 - commission_total
            trade = Trade(
                account_id=account.id,
                ticker_id=ticker.id,
                ticker=ticker.ticker,
                trade_type_id=trade_type.id,
                trade_type="ROCT CALL",
                date_trade_open=open_date,
                expiration_date=expiration,
                days_to_expiration=(expiration - open_date).days,
                num_of_contracts=contracts,
                strike_price=strike,
                price_per_share=open_price,
                credit_debit=open_price,
                total_premium=total_premium,
                commission_per_share=commission_per_share,
                total_amount=total_premium,
                notes=NOTE,
                import_batch=BATCH,
            )
            session.add(trade)
            session.flush()
            session.add(
                TradeEvent(
                    trade_id=trade.id,
                    event_type="OPEN",
                    event_date=open_date,
                    import_batch=BATCH,
                )
            )
            if close_kind == "EXPIRE":
                session.add(
                    TradeEvent(
                        trade_id=trade.id,
                        event_type="EXPIRE",
                        event_date=close_date,
                        closing_debit=Decimal("0"),
                        total_debit=Decimal("0"),
                        notes="Expired worthless.",
                        import_batch=BATCH,
                    )
                )
            else:
                total_debit = close_price * Decimal(contracts) * 100 + commission_total
                session.add(
                    TradeEvent(
                        trade_id=trade.id,
                        event_type="CLOSE",
                        event_date=close_date,
                        closing_debit=close_price,
                        total_debit=total_debit,
                        notes="Bought to close.",
                        import_batch=BATCH,
                    )
                )
            created += 1

        if args.dry_run:
            session.rollback()
        else:
            session.commit()

    print(f"{'would ' if args.dry_run else ''}create: {created} option trades")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
