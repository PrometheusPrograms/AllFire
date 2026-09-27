"""Backfill cash dividends from Schwab/TDA statement PDFs into `cash_flows`.

Statements are ground truth for cash received. Three repairs, in one pass:

1. **Relabel interest.** The Schwab API import mapped `DIVIDEND_OR_INTEREST` to
   `DIVIDEND`, so bank-sweep interest ("BANK INT ... TD BANK USA NA",
   "SCHWAB1 INT ...") and margin-interest adjustments ("MGN INT ADJ") were
   booked as dividends. Those rows become `INTEREST`.
2. **Link dividends to tickers.** The same import stored dividends with only a
   description and `ticker_id = NULL`, so nothing showed up in the ticker drawer
   or reduced cost basis. Each statement dividend is matched to an existing row
   on (account, date, amount); the statement's symbol supplies the ticker.
3. **Insert what's missing.** Cash dividends with no DB row at all —
   everything before the API import's 2024 start (TDA 2013-2023, account 641,
   2023 H2) — are inserted with `import_batch='stmt_dividends'`.
4. **Drop reinvested dividends.** Reinvested (DRIP) dollars are never
   cash_flows; the shares they bought are a consolidated whole-share catch-up
   BTO instead. Any DB dividend matching a reinvested statement dividend is
   removed, and none are inserted.

Account mapping: 742 -> Rule 1, 467 -> Roth, 641 -> Rule 1 (641 was
reconstructed into Rule 1; see PRODUCTION_IMPORT_RUNBOOK.md).

    python -m scripts.backfill_statement_dividends --database-url ... \\
        --statements-dir "/path/to/Schwab statements" [--dry-run]

Idempotent: a re-run matches the rows it inserted/linked last time and changes
nothing.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Account, CashFlow, Ticker
from scripts.okw_loader import get_or_create_ticker

IMPORT_BATCH = "stmt_dividends"
STATEMENT_ACCOUNTS = {"742": "Rule 1", "467": "Roth", "641": "Rule 1"}

# Current-format (2023+) Transaction Details: Category "Dividend" + these actions.
CURRENT_CASH_DIVIDEND_ACTIONS = {
    "CashDividend",
    "Qual.Dividend",
    "SpecQualDiv",
    "PrYrCashDiv",
    "PrYrSpecialDiv",
}
CURRENT_REINVESTED_DIVIDEND_ACTIONS = {"DivForReinvest", "QualDivReinvest"}
# Legacy TDA statements don't label reinvestment: the dividend's cash buys the
# same symbol within a few days for the same dollars (a fractional "Buy").
REINVEST_MATCH_DAYS = timedelta(days=3)
REINVEST_MATCH_AMOUNT = Decimal("0.05")
# Legacy TDA Account Activity: dividends are "Div/Int - Income". Interest
# ("Div/Int - Other" on MMDA1), margin interest ("- Expense") and split
# cash-in-lieu ("- Securities") are deliberately excluded.
LEGACY_DIVIDEND_ACTIVITY = "Div/Int - Income"

INTEREST_DESCRIPTION_RE = re.compile(r"\b(BANK INT|SCHWAB1 INT|MGN INT)\b")
# Schwab posts the same dividend on the pay date, but the API sometimes dates it
# a few days off; only used when no exact-date row exists.
DATE_TOLERANCE = timedelta(days=5)
CENT = Decimal("0.01")


@dataclass(frozen=True)
class StatementDividend:
    source_account: str  # statement account number (742/467/641)
    account_name: str
    symbol: str
    paid_on: date
    amount: Decimal
    description: str
    # Reinvested (DRIP): the cash went straight into fractional shares. Never a
    # cash_flow — those dollars live in the consolidated whole-share catch-up
    # BTO instead (see scripts/manual_entry.py / statement_gap_lots.json).
    reinvested: bool = False


@dataclass
class DbDividend:
    id: int
    account_name: str
    ticker: str | None
    paid_on: date
    amount: Decimal
    description: str | None


@dataclass
class DividendPlan:
    relabel_interest: list[int] = field(default_factory=list)
    link: list[tuple[int, str]] = field(default_factory=list)  # (cash_flow id, symbol)
    insert: list[StatementDividend] = field(default_factory=list)
    remove_reinvested: list[int] = field(default_factory=list)  # cash_flow ids
    unmatched_db: list[DbDividend] = field(default_factory=list)


def _clean_symbol(raw: str | None) -> str | None:
    """Legacy TDA rows sometimes glue a description word onto the symbol
    ("INC WFM", "CO INC AWK") — the ticker is the last token."""
    if not raw:
        return None
    token = raw.split()[-1].strip().upper()
    return token if token.isalpha() else None


def _cents(value: Decimal) -> Decimal:
    return value.quantize(CENT)


def _words(text: str | None) -> set[str]:
    return set(re.findall(r"[A-Z0-9]+", (text or "").upper()))


def _similarity(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def dividends_from_current_rows(source_account: str, transactions) -> list[StatementDividend]:
    """`transactions` are `schwab_statement_parser.TxnRow`s."""
    account_name = STATEMENT_ACCOUNTS[source_account]
    out = []
    for txn in transactions:
        if txn.category != "Dividend":
            continue
        reinvested = txn.action in CURRENT_REINVESTED_DIVIDEND_ACTIONS
        if not reinvested and txn.action not in CURRENT_CASH_DIVIDEND_ACTIONS:
            continue
        symbol = _clean_symbol(txn.symbol_cusip)
        if symbol is None or txn.date is None or txn.amount is None:
            continue
        out.append(
            StatementDividend(
                source_account,
                account_name,
                symbol,
                txn.date,
                _cents(txn.amount),
                txn.description,
                reinvested,
            )
        )
    return out


def dividends_from_legacy_rows(source_account: str, transactions) -> list[StatementDividend]:
    """`transactions` are `schwab_legacy_statement_parser.LegacyTxnRow`s."""
    account_name = STATEMENT_ACCOUNTS[source_account]
    buys: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
    for txn in transactions:
        symbol = _clean_symbol(txn.symbol)
        bought_on = txn.trade_date or txn.settle_date
        if (
            (txn.activity or "").startswith("Buy")
            and symbol
            and bought_on
            and txn.amount is not None
        ):
            buys[symbol].append((bought_on, abs(txn.amount)))

    out = []
    for txn in transactions:
        if txn.activity != LEGACY_DIVIDEND_ACTIVITY:
            continue
        symbol = _clean_symbol(txn.symbol)
        paid_on = txn.trade_date or txn.settle_date
        if symbol is None or paid_on is None or txn.amount is None:
            continue
        amount = _cents(txn.amount)
        reinvested = any(
            abs(bought_on - paid_on) <= REINVEST_MATCH_DAYS
            and abs(spent - abs(amount)) <= REINVEST_MATCH_AMOUNT
            for bought_on, spent in buys[symbol]
        )
        out.append(
            StatementDividend(
                source_account, account_name, symbol, paid_on, amount, txn.description, reinvested
            )
        )
    return out


def dedupe_statement_dividends(dividends: list[StatementDividend]) -> list[StatementDividend]:
    """The same row can print on two overlapping statements of one account.
    Keyed by source account too, so 641 and 742 (both Rule 1) holding the same
    ticker on the same day keep both genuine payments."""
    seen: dict[tuple, StatementDividend] = {}
    for div in dividends:
        seen.setdefault((div.source_account, div.symbol, div.paid_on, div.amount), div)
    return sorted(seen.values(), key=lambda d: (d.paid_on, d.account_name, d.symbol))


def plan_dividend_backfill(
    statement_dividends: list[StatementDividend], db_rows: list[DbDividend]
) -> DividendPlan:
    """Pure matching: decide which DB rows to relabel/link and which statement
    dividends to insert. No DB access, so it's unit-testable."""
    plan = DividendPlan()
    candidates: list[DbDividend] = []
    for row in db_rows:
        if row.ticker is None and INTEREST_DESCRIPTION_RE.search(row.description or ""):
            plan.relabel_interest.append(row.id)
        else:
            candidates.append(row)

    by_key: dict[tuple[str, Decimal], list[DbDividend]] = defaultdict(list)
    for row in candidates:
        by_key[(row.account_name, _cents(row.amount))].append(row)
    claimed: set[int] = set()

    # Description -> symbol (and the reverse), learned from matches below; breaks
    # ties when several same-day, same-amount rows exist for different tickers.
    description_symbol: dict[str, str] = {}
    known: dict[str, set[str]] = defaultdict(set)

    def pick(div: StatementDividend, *, exact: bool, lenient: bool) -> DbDividend | None:
        pool = [
            row
            for row in by_key[(div.account_name, div.amount)]
            if row.id not in claimed
            and (
                row.paid_on == div.paid_on
                if exact
                else abs(row.paid_on - div.paid_on) <= DATE_TOLERANCE
            )
            and (row.ticker is None or row.ticker == div.symbol)
        ]
        if not pool:
            return None
        for row in pool:  # already linked to this ticker
            if row.ticker == div.symbol:
                return row
        for row in pool:  # description previously seen for this symbol
            if description_symbol.get(row.description or "") == div.symbol:
                return row
        if len({row.description for row in pool}) == 1:
            return pool[0]
        if not lenient:
            return None

        # Every row in the pool has this exact account, date and amount, so any
        # pairing keeps per-ticker totals right (far better than inserting a
        # duplicate). Prefer the row that looks most like this symbol's known
        # descriptions and least like another symbol's — "ALPHABET INC CLASS A"
        # vs a learned "ALPHABET INC CLASS CLASS A" for GOOGL.
        def fit(row: DbDividend, symbol: str) -> float:
            words = _words(row.description)
            return max((_similarity(words, _words(d)) for d in known[symbol]), default=0.0)

        return max(
            pool,
            key=lambda row: (
                fit(row, div.symbol),
                -max((fit(row, other) for other in known if other != div.symbol), default=0.0),
            ),
        )

    # Strict passes first (unambiguous matches only) so descriptions are learned
    # before same-day, same-amount ties are resolved — statements print e.g.
    # GOOG and GOOGL both as "ALPHABETINC", but a later GOOGL-only payment
    # teaches which DB description ("... CLASS A") belongs to which symbol.
    unmatched: list[StatementDividend] = list(statement_dividends)
    for lenient, exact in ((False, True), (False, False), (True, True), (True, False)):
        pending, unmatched = unmatched, []
        for div in pending:
            row = pick(div, exact=exact, lenient=lenient)
            if row is None:
                unmatched.append(div)
                continue
            claimed.add(row.id)
            if row.description:
                description_symbol.setdefault(row.description, div.symbol)
                known[div.symbol].add(row.description)
            if div.reinvested:
                plan.remove_reinvested.append(row.id)
            elif row.ticker is None:
                plan.link.append((row.id, div.symbol))
    plan.insert = [div for div in unmatched if not div.reinvested]

    for row in candidates:
        if row.id in claimed:
            continue
        symbol = description_symbol.get(row.description or "")
        if row.ticker is None and symbol is not None:
            plan.link.append((row.id, symbol))
        elif row.ticker is None:
            plan.unmatched_db.append(row)
    return plan


def read_statement_dividends(statements_dir: Path) -> list[StatementDividend]:
    # Imported lazily: pdfplumber is only needed when actually reading PDFs.
    from scripts.schwab_legacy_statement_parser import parse_legacy_statement
    from scripts.schwab_statement_parser import parse_statement

    dividends: list[StatementDividend] = []
    # Legacy rows are pooled per account, not per PDF: a month-end dividend's
    # reinvestment buy can post on the next month's statement.
    legacy_rows: dict[str, list] = defaultdict(list)
    for pdf in sorted(p for p in statements_dir.iterdir() if p.suffix.lower() == ".pdf"):
        source_account = pdf.stem.rsplit("_", 1)[-1]
        if source_account not in STATEMENT_ACCOUNTS:
            continue
        if pdf.name.startswith("TDA"):
            legacy_rows[source_account] += parse_legacy_statement(pdf).transactions
        else:
            parsed = parse_statement(pdf)
            dividends += dividends_from_current_rows(source_account, parsed.transactions)
    for source_account, transactions in legacy_rows.items():
        dividends += dividends_from_legacy_rows(source_account, transactions)
    return dedupe_statement_dividends(dividends)


def _db_dividends(session: Session) -> list[DbDividend]:
    rows = session.execute(
        select(CashFlow, Account.account_name, Ticker.ticker)
        .join(Account, Account.id == CashFlow.account_id)
        .outerjoin(Ticker, Ticker.id == CashFlow.ticker_id)
        .where(CashFlow.transaction_type == "DIVIDEND")
    ).all()
    return [
        DbDividend(
            flow.id, account_name, ticker, flow.transaction_date, flow.amount, flow.description
        )
        for flow, account_name, ticker in rows
    ]


def apply_plan(session: Session, plan: DividendPlan) -> None:
    for flow_id in plan.relabel_interest:
        session.get(CashFlow, flow_id).transaction_type = "INTEREST"
    for flow_id in plan.remove_reinvested:
        session.delete(session.get(CashFlow, flow_id))
    for flow_id, symbol in plan.link:
        session.get(CashFlow, flow_id).ticker_id = get_or_create_ticker(session, symbol).id
    account_ids = dict(session.execute(select(Account.account_name, Account.id)).all())
    for div in plan.insert:
        session.add(
            CashFlow(
                account_id=account_ids[div.account_name],
                ticker_id=get_or_create_ticker(session, div.symbol).id,
                transaction_date=div.paid_on,
                transaction_type="DIVIDEND",
                amount=div.amount,
                description=div.description,
                import_batch=IMPORT_BATCH,
            )
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--statements-dir", required=True, type=Path)
    parser.add_argument(
        "--backup",
        type=Path,
        default=Path("removed_reinvested_dividends.json"),
        help="Where to write the cash_flows rows removed as reinvested (DRIP).",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    statement_dividends = read_statement_dividends(args.statements_dir)
    engine = create_engine(args.database_url)
    with sessionmaker(bind=engine)() as session:
        plan = plan_dividend_backfill(statement_dividends, _db_dividends(session))
        if not args.dry_run:
            if plan.remove_reinvested:
                removed = [
                    {c.name: getattr(flow, c.name) for c in CashFlow.__table__.columns}
                    for flow in session.scalars(
                        select(CashFlow).where(CashFlow.id.in_(plan.remove_reinvested))
                    )
                ]
                args.backup.write_text(json.dumps(removed, default=str, indent=1))
                print(f"backed up {len(removed)} reinvested dividend row(s) -> {args.backup}")
            apply_plan(session, plan)
            session.commit()

    verb = "would" if args.dry_run else "did"
    inserted_total = sum((d.amount for d in plan.insert), start=Decimal("0"))
    print(f"statement dividends read: {len(statement_dividends)}")
    print(f"{verb} relabel {len(plan.relabel_interest)} interest row(s) DIVIDEND -> INTEREST")
    print(f"{verb} link {len(plan.link)} existing dividend(s) to their ticker")
    print(f"{verb} insert {len(plan.insert)} missing cash dividend(s), ${inserted_total}")
    print(f"{verb} remove {len(plan.remove_reinvested)} reinvested (DRIP) dividend row(s)")
    by_year: dict[int, int] = defaultdict(int)
    for div in plan.insert:
        by_year[div.paid_on.year] += 1
    print(f"  inserts by year: {dict(sorted(by_year.items()))}")
    if plan.unmatched_db:
        print(
            f"DB dividends with no statement match and no known ticker ({len(plan.unmatched_db)}):"
        )
        for row in plan.unmatched_db:
            print(
                f"  cash_flow {row.id} {row.account_name} {row.paid_on} {row.amount} "
                f"{row.description}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
