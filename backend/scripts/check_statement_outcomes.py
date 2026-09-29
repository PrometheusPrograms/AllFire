"""Read-only: check every single-leg option trade against the statement PDFs.

Statements are ground truth (see PRODUCTION_IMPORT_RUNBOOK.md §3). For each
contract — (account, ticker, expiration, strike, PUT/CALL) — this builds the
statement ledger (contracts sold, bought back, assigned, expired) and compares
it with the OKW-imported trades:

- contract count: DB contracts vs contracts sold short on the statement
  (contracts *bought* before the first sale are a spread's long leg, and its
  later closing sales are not short contracts)
- fill price: DB credit vs the statement's sale price, when there is one DB
  row, it is not a roll continuation (those store the roll's net), and every
  short sale filled at one price
- outcome: DB ASSIGN/EXPIRE vs the statement's "Option … Assignment" /
  "ExpiredLong/Short" lines, including partial assignment (some contracts
  assigned, the rest expired)
- strike typo: a DB contract with no statement activity where the statement
  has an unmatched contract of the same ticker/expiration/right
- early assignment: the statement's assignment line is dated before
  expiration, so the ASSIGN event (and its share lot) should carry that date
- unresolved: a trade (spreads included, by their short leg) past its
  expiration with no ROLL/EXPIRE/CLOSE/ASSIGN event. Those are the only four
  outcomes; the statement decides which — bought back with a same-day sale of
  the same ticker/right is a ROLL, bought back alone is a CLOSE (both dated one
  business day before the statement's settlement date). Anything the
  statements can't settle is listed as `ask` for a person; contracts expiring
  after the newest statement are listed as `needs_api` (use
  `scripts.check_schwab_activity`).

It never writes to the database. It prints a report and writes *proposed*
correction entries (same shape as `scripts/data/statement_corrections.json`)
for a person to review before anything is applied.

Apart from the unresolved check, spreads are skipped (two legs, closed as a
unit); so are contracts expiring after the newest statement.

    python -m scripts.check_statement_outcomes --database-url ... \\
        --statements-dir "/path/to/Schwab statements" [--out proposed.json]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
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
SPREAD_TYPES = {"ROCS BULL PUT SPREAD", "BULL PUT SPREAD"}
CLOSING_EVENTS = {"ROLL", "EXPIRE", "CLOSE", "ASSIGN"}

ContractKey = tuple[str, str, date, Decimal, str]  # account, ticker, exp, strike, PUT|CALL


@dataclass
class StatementContract:
    sold: Decimal = ZERO
    bought: Decimal = ZERO
    assigned: Decimal = ZERO
    expired: Decimal = ZERO
    assigned_on: date | None = None
    sold_on: list[date] = field(default_factory=list)
    bought_on: list[date] = field(default_factory=list)
    sale_prices: list[Decimal] = field(default_factory=list)

    @property
    def long_leg(self) -> Decimal:
        """Contracts bought before any sale: a spread's long leg, not a buyback."""
        if not self.bought_on or not self.sold_on or min(self.bought_on) >= min(self.sold_on):
            return ZERO
        return self.bought

    @property
    def sold_short(self) -> Decimal:
        return self.sold - self.long_leg
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
                    contract.sold_on.append(row.date)
                    if row.price is not None:
                        contract.sale_prices.append(Decimal(row.price))
                elif category == "Purchase":
                    contract.bought += qty
                    contract.bought_on.append(row.date)

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


def _previous_business_day(day: date) -> date:
    """Statement Sale/Purchase dates are settlement (T+1); the trade was the
    business day before (e.g. ADBE roll traded 2026-08-20, statement 08/21).
    Exchange holidays are not modelled — the proposal is reviewed anyway."""
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def resolve_outcome(
    ledger: dict[ContractKey, StatementContract], key: ContractKey, contracts: Decimal
) -> tuple[str, date] | None:
    """The one outcome the statements show for `contracts` short contracts at
    `key`, or None when they don't settle it (partial, missing, conflicting)."""
    stmt = ledger.get(key)
    if stmt is None or contracts <= ZERO:
        return None
    account_name, ticker, expiration, _strike, right = key
    if stmt.assigned == contracts and not stmt.expired and not stmt.bought:
        return "ASSIGN", min(stmt.assigned_on or expiration, expiration)
    if stmt.expired == contracts and not stmt.assigned and not stmt.bought:
        return "EXPIRE", expiration
    if stmt.bought == contracts and not stmt.assigned and not stmt.expired and stmt.bought_on:
        settled = max(stmt.bought_on)
        rolled = any(
            settled in other.sold_on
            for other_key, other in ledger.items()
            if other_key != key and other_key[:2] == key[:2] and other_key[4] == right
        )
        return ("ROLL" if rolled else "CLOSE"), _previous_business_day(settled)
    return None


def unresolved(
    ledger: dict[ContractKey, StatementContract],
    trades: list[tuple[Trade, str, list[TradeEvent]]],
    coverage_end: date,
    today: date,
) -> list[Finding]:
    """Trades past expiration with no outcome event, resolved from statements."""
    findings = []
    for trade, account_name, events in trades:
        if trade.expiration_date is None or trade.expiration_date >= today:
            continue
        if any(e.event_type in CLOSING_EVENTS for e in events):
            continue
        right = "PUT" if "PUT" in trade.trade_type else "CALL"
        key = (account_name, trade.ticker, trade.expiration_date, trade.strike_price, right)
        label = "spread short leg" if trade.long_strike is not None else "contract"
        if trade.expiration_date > coverage_end:
            findings.append(
                Finding(
                    "needs_api",
                    key,
                    f"past expiration, no outcome, after the newest statement ({coverage_end}); "
                    "check with scripts.check_schwab_activity",
                    [trade.id],
                )
            )
            continue
        resolved = resolve_outcome(ledger, key, Decimal(trade.num_of_contracts or 0))
        if resolved is None:
            stmt = ledger.get(key, StatementContract())
            findings.append(
                Finding(
                    "ask",
                    key,
                    f"past expiration with no outcome; statement {label}: sold {stmt.sold}, "
                    f"bought back {stmt.bought}, expired {stmt.expired}, assigned {stmt.assigned}",
                    [trade.id],
                )
            )
            continue
        event_type, on = resolved
        findings.append(
            Finding(
                "unresolved",
                key,
                f"past expiration with no outcome; statement {label} shows {event_type} {on}",
                [trade.id],
                {"events": {event_type: str(on)}},
            )
        )
    return findings


def _natural_key(trade: Trade, account_name: str) -> dict[str, Any]:
    return {
        "account": account_name,
        "ticker": trade.ticker,
        "trade_type": trade.trade_type,
        "date_trade_open": str(trade.date_trade_open),
        "expiration_date": str(trade.expiration_date),
        "strike_price": str(trade.strike_price),
    }


def continuation_ids(trades: list[tuple[Trade, str, list[TradeEvent]]]) -> set[int]:
    """Trades whose OKW credit is by convention not their own fill: the new leg
    of a roll (OKW stores the roll's net) and the put a spread converts into
    (OKW stores the long leg's closing sale). Recognized by a ROLL, or a
    spread's CLOSE, of the same account/ticker/right on the open date or up to
    three days before."""
    ends: dict[tuple[str, str, str], list[date]] = defaultdict(list)
    for trade, account_name, events in trades:
        right = "PUT" if "PUT" in trade.trade_type else "CALL"
        for event in events:
            if event.event_type == "ROLL" or (
                event.event_type == "CLOSE" and trade.long_strike is not None
            ):
                ends[(account_name, trade.ticker, right)].append(event.event_date)
    found = set()
    for trade, account_name, _ in trades:
        right = "PUT" if "PUT" in trade.trade_type else "CALL"
        opened = trade.date_trade_open
        if trade.trade_parent_id is not None or any(
            timedelta(0) <= opened - ended <= timedelta(days=3)
            for ended in ends[(account_name, trade.ticker, right)]
        ):
            found.add(trade.id)
    return found


def compare(
    ledger: dict[ContractKey, StatementContract],
    trades: list[tuple[Trade, str, list[TradeEvent]]],
    coverage_end: date,
    continuations: set[int] | frozenset[int] = frozenset(),
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
        # A trade with no outcome at all is `unresolved()`'s job; comparing its
        # (empty) outcome here would mislabel it and propose a no-op swap.
        rows = [(t, ev) for t, ev in rows if any(e.event_type in CLOSING_EVENTS for e in ev)]
        if not rows:
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

        if db_contracts != stmt.sold_short:
            findings.append(
                Finding(
                    "contract_count",
                    key,
                    f"DB {db_contracts} contracts vs statement sold {stmt.sold_short}",
                    ids,
                    {
                        "set": {
                            "num_of_contracts": int(stmt.sold_short),
                            "num_of_shares": int(stmt.sold_short * 100),
                        },
                        "recompute": True,
                    }
                    if len(rows) == 1
                    else None,
                )
            )

        if (
            len(rows) == 1
            and getattr(rows[0][0], "trade_parent_id", None) is None
            and rows[0][0].id not in continuations
            and not stmt.long_leg
            and len(set(stmt.sale_prices)) == 1
        ):
            # Report-only: a same-day buyback of the same ticker/right is not
            # proof of a roll (it is often an unrelated close), so rolls OKW
            # didn't mark must be confirmed by a person before any change.
            price, what = stmt.sale_prices[0], "fill"
            if Decimal(rows[0][0].credit_debit) != price:
                findings.append(
                    Finding(
                        "fill_price",
                        key,
                        f"DB credit {rows[0][0].credit_debit} vs statement {what} {price}",
                        ids,
                        {
                            "match": {"credit_debit": str(rows[0][0].credit_debit)},
                            "set": {"credit_debit": str(price)},
                            "recompute": True,
                        },
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
                # Swap whatever outcome the DB has (usually EXPIRE; OKW
                # sometimes says CLOSE) — never assume it.
                kind, proposal = (
                    "expired_but_assigned" if outcome(rows[0][1]) == "EXPIRE" else "closed_but_assigned",
                    {
                        "outcome": {
                            "from": outcome(rows[0][1]),
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


def _load_trades(
    session: Session, since: date, types: set[str] = SINGLE_LEG_TYPES
) -> list[tuple[Trade, str, list[TradeEvent]]]:
    rows = session.execute(
        select(Trade, Account.account_name)
        .join(Account, Account.id == Trade.account_id)
        .where(Trade.trade_type.in_(types), Trade.expiration_date >= since)
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
        with_spreads = _load_trades(session, coverage_start, SINGLE_LEG_TYPES | SPREAD_TYPES)
        findings = compare(
            ledger, trades, coverage_end, continuation_ids(with_spreads)
        ) + unresolved(
            ledger, with_spreads, coverage_end, date.today()
        )
        accounts_by_trade = {t.id: a for t, a, _ in with_spreads}
        trades_by_id = {t.id: t for t, _, _ in with_spreads}

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
