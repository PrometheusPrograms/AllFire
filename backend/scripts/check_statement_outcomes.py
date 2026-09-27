"""Read-only: check every single-leg option trade against the statement PDFs.

Statements are ground truth (see PRODUCTION_IMPORT_RUNBOOK.md §3). For each
contract — (account, ticker, expiration, strike, PUT/CALL) — this builds the
statement ledger (contracts sold, bought back, assigned, expired) and compares
it with the OKW-imported trades:

- contract count: DB contracts vs contracts sold on the statement
- outcome: DB ASSIGN/EXPIRE vs the statement's "Option … Assignment" /
  "ExpiredLong/Short" lines, including partial assignment (some contracts
  assigned, the rest expired)
- strike typo: a DB contract with no statement activity where the statement
  has an unmatched contract of the same ticker/expiration/right
- early assignment: the statement's assignment line is dated before
  expiration, so the ASSIGN event (and its share lot) should carry that date

It never writes to the database. It prints a report and writes *proposed*
correction entries (same shape as `scripts/data/statement_corrections.json`)
for a person to review before anything is applied.

Spreads are skipped (two legs, closed as a unit); so are contracts expiring
after the newest statement.

    python -m scripts.check_statement_outcomes --database-url ... \\
        --statements-dir "/path/to/Schwab statements" [--out proposed.json]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Account, Trade, TradeEvent
from scripts.reconcile_statements import (
    parse_option_identity,
    parse_statement_dir,
    row_quantity,
)

ZERO = Decimal("0")
STATEMENT_ACCOUNTS = {"742": "Rule 1", "467": "Roth", "641": "Rule 1"}
SINGLE_LEG_TYPES = {"ROCT PUT", "RULE ONE PUT", "ROCT CALL", "RULE ONE CALL"}

ContractKey = tuple[str, str, date, Decimal, str]  # account, ticker, exp, strike, PUT|CALL


@dataclass
class StatementContract:
    sold: Decimal = ZERO
    bought: Decimal = ZERO
    assigned: Decimal = ZERO
    expired: Decimal = ZERO
    assigned_on: date | None = None
    sources: list[str] = field(default_factory=list)


@dataclass
class Finding:
    kind: str
    key: ContractKey
    detail: str
    trade_ids: list[int]
    proposal: dict[str, Any] | None = None


def statement_ledger(
    statements_by_account: dict[str, list[Any]],
) -> dict[ContractKey, StatementContract]:
    ledger: dict[ContractKey, StatementContract] = defaultdict(StatementContract)
    for account_name, statements in statements_by_account.items():
        for parsed in statements:
            for row in parsed.transactions:
                ident = parse_option_identity(row.symbol_cusip, row.description)
                if ident is None or row.date is None:
                    continue
                ticker, expiration, strike, right = ident
                qty = row_quantity(row)
                if qty is None:
                    continue
                contract = ledger[(account_name, ticker, expiration, strike, right)]
                action = (row.action or "").lower()
                category = (row.category or "").strip()
                if "expired" in action:
                    contract.expired += qty
                elif category == "Sale":
                    contract.sold += qty
                elif category == "Purchase":
                    contract.bought += qty
                elif category == "Other" and "option" in action:
                    contract.assigned += qty
                    contract.assigned_on = min(filter(None, (contract.assigned_on, row.date)))
                else:
                    continue
                contract.sources.append(
                    f"{row.date} {category} {row.action or ''} {qty} "
                    f"({Path(parsed.meta.source_file).name})"
                )
    return ledger


def _natural_key(trade: Trade, account_name: str) -> dict[str, Any]:
    return {
        "account": account_name,
        "ticker": trade.ticker,
        "trade_type": trade.trade_type,
        "date_trade_open": str(trade.date_trade_open),
        "expiration_date": str(trade.expiration_date),
        "strike_price": str(trade.strike_price),
    }


def compare(
    ledger: dict[ContractKey, StatementContract],
    trades: list[tuple[Trade, str, list[TradeEvent]]],
    coverage_end: date,
) -> list[Finding]:
    findings: list[Finding] = []
    by_key: dict[ContractKey, list[tuple[Trade, list[TradeEvent]]]] = defaultdict(list)
    for trade, account_name, events in trades:
        right = "PUT" if "PUT" in trade.trade_type else "CALL"
        by_key[
            (account_name, trade.ticker, trade.expiration_date, trade.strike_price, right)
        ].append((trade, events))

    def outcome(events: list[TradeEvent]) -> str | None:
        closing = [e for e in events if e.event_type != "OPEN"]
        return closing[-1].event_type if closing else None

    matched_statement_keys: set[ContractKey] = set()
    for key, rows in sorted(by_key.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])):
        account_name, ticker, expiration, strike, right = key
        if expiration > coverage_end:
            continue
        ids = [t.id for t, _ in rows]
        db_contracts = sum((Decimal(t.num_of_contracts or 0) for t, _ in rows), ZERO)
        stmt = ledger.get(key)
        if stmt is None or stmt.sold == ZERO:
            sibling = [
                k
                for k, c in ledger.items()
                if k[:3] == key[:3] and k[4] == right and k not in by_key and c.sold > ZERO
            ]
            if len(sibling) == 1:
                findings.append(
                    Finding(
                        "strike_typo",
                        key,
                        f"no statement activity at strike {strike}; statement has {ticker} "
                        f"{right} {sibling[0][3]} exp {expiration} "
                        f"({ledger[sibling[0]].sold} sold)",
                        ids,
                        {"set": {"strike_price": str(sibling[0][3])}},
                    )
                )
                matched_statement_keys.add(sibling[0])
            else:
                findings.append(Finding("no_fill", key, "no statement sale for this contract", ids))
            continue
        matched_statement_keys.add(key)

        if db_contracts != stmt.sold:
            findings.append(
                Finding(
                    "contract_count",
                    key,
                    f"DB {db_contracts} contracts vs statement sold {stmt.sold}",
                    ids,
                    {
                        "set": {
                            "num_of_contracts": int(stmt.sold),
                            "num_of_shares": int(stmt.sold * 100),
                        },
                        "recompute": True,
                    }
                    if len(rows) == 1
                    else None,
                )
            )

        db_assigned = sum(
            (Decimal(t.num_of_contracts or 0) for t, ev in rows if outcome(ev) == "ASSIGN"), ZERO
        )
        if db_assigned != stmt.assigned:
            if stmt.assigned == ZERO and stmt.expired > ZERO:
                kind, proposal = (
                    "assigned_but_expired",
                    {"outcome": {"from": "ASSIGN", "to": "EXPIRE", "date": str(expiration)}},
                )
            elif db_assigned == ZERO and stmt.assigned > ZERO and stmt.expired == ZERO:
                kind, proposal = (
                    "expired_but_assigned",
                    {
                        "outcome": {
                            "from": "EXPIRE",
                            "to": "ASSIGN",
                            "date": str(min(stmt.assigned_on or expiration, expiration)),
                        }
                    }
                    if len(rows) == 1
                    else None,
                )
            elif ZERO < stmt.assigned < db_contracts and len(rows) == 1:
                kind = "partial_assignment"
                proposal = {
                    "match": {"num_of_contracts": int(db_contracts)},
                    "split": {
                        "assigned_contracts": int(stmt.assigned),
                        "expired_contracts": int(stmt.expired),
                        "expire_date": str(expiration),
                    },
                }
            else:
                kind, proposal = "assignment_count", None
            findings.append(
                Finding(
                    kind,
                    key,
                    f"DB assigned {db_assigned} contract(s); statement assigned {stmt.assigned}, "
                    f"expired {stmt.expired}, bought back {stmt.bought}",
                    ids,
                    proposal,
                )
            )
        elif stmt.assigned > ZERO and stmt.assigned_on and stmt.assigned_on < expiration:
            events = [e for _, ev in rows for e in ev if e.event_type == "ASSIGN"]
            if any(e.event_date != stmt.assigned_on for e in events):
                findings.append(
                    Finding(
                        "early_assignment_date",
                        key,
                        f"statement assignment dated {stmt.assigned_on} (before expiration "
                        f"{expiration}); DB ASSIGN on "
                        f"{sorted({str(e.event_date) for e in events})}",
                        ids,
                        {"events": {"ASSIGN": str(stmt.assigned_on)}},
                    )
                )
    return findings


def _load_trades(session: Session, since: date) -> list[tuple[Trade, str, list[TradeEvent]]]:
    rows = session.execute(
        select(Trade, Account.account_name)
        .join(Account, Account.id == Trade.account_id)
        .where(Trade.trade_type.in_(SINGLE_LEG_TYPES), Trade.expiration_date >= since)
    ).all()
    events: dict[int, list[TradeEvent]] = defaultdict(list)
    for event in session.scalars(
        select(TradeEvent)
        .where(TradeEvent.trade_id.in_([t.id for t, _ in rows]))
        .order_by(TradeEvent.event_date, TradeEvent.id)
    ):
        events[event.trade_id].append(event)
    return [(trade, account_name, events[trade.id]) for trade, account_name in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--statements-dir", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=Path("proposed_statement_corrections.json"))
    args = parser.parse_args(argv)

    statements: dict[str, list[Any]] = defaultdict(list)
    for suffix, account_name in STATEMENT_ACCOUNTS.items():
        statements[account_name] += parse_statement_dir(args.statements_dir, f"_{suffix}")
    periods = [s.meta for sts in statements.values() for s in sts if s.meta.period_end]
    coverage_start = min(m.period_start or m.period_end for m in periods)
    coverage_end = max(m.period_end for m in periods)
    ledger = statement_ledger(statements)

    with sessionmaker(bind=create_engine(args.database_url))() as session:
        trades = _load_trades(session, coverage_start)
        findings = compare(ledger, trades, coverage_end)
        accounts_by_trade = {t.id: a for t, a, _ in trades}
        trades_by_id = {t.id: t for t, _, _ in trades}

    print(
        f"statements {coverage_start} .. {coverage_end}; "
        f"single-leg option trades checked: {len(trades)}"
    )
    counts: dict[str, int] = defaultdict(int)
    proposals = []
    for f in findings:
        counts[f.kind] += 1
        account_name, ticker, expiration, strike, right = f.key
        print(
            f"[{f.kind}] {account_name} {ticker} {right} {strike} exp {expiration} "
            f"trades {f.trade_ids}: {f.detail}"
        )
        if f.proposal is not None:
            first = trades_by_id[f.trade_ids[0]]
            proposals.append(
                {
                    "id": f"{ticker.lower()}-{expiration}-{f.kind}",
                    **_natural_key(first, accounts_by_trade[first.id]),
                    **({"expect": len(f.trade_ids)} if len(f.trade_ids) > 1 else {}),
                    **f.proposal,
                    "statement": "; ".join(ledger.get(f.key, StatementContract()).sources[:6]),
                    "reason": f"{f.kind}: {f.detail}",
                }
            )
    print("summary:", dict(sorted(counts.items())))
    args.out.write_text(json.dumps({"proposed": proposals}, indent=2, default=str))
    print(f"wrote {len(proposals)} proposed correction(s) for review -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
