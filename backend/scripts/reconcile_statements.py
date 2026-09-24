"""Read-only OKW / cost_basis vs Schwab statement reconciliation.

Statements are ground truth for fills. OKW is the strategy log. This CLI
flags missing/extra trades, RESULT mismatches, strike/exp/premium drift,
and share-count holes (the LULU 9-vs-10 class of bug).

    python -m scripts.reconcile_statements --database-url ... \\
        --statements-dir "/path/to/Schwab statements" \\
        --account rule1 --ticker LULU

Without --statements-dir, still checks DB cost_basis against the bundled
LULU expected-lots fixture when --ticker LULU.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Account, CostBasis, Ticker, Trade, TradeEvent
from scripts.schwab_statement_parser import TxnRow, parse_statement

ZERO = Decimal("0")
PRICE_TOLERANCE = Decimal("0.05")
TRADE_DATE_WINDOW = timedelta(days=7)
DEFAULT_LULU_FIXTURE = Path(__file__).with_name("data") / "lulu_rule1_expected_lots.json"

ACCOUNTS = {
    "rule1": ("Rule 1", "742"),
    "roth": ("Roth", "467"),
}

OPTION_SYMBOL_RE = re.compile(
    r"^(?P<ticker>[A-Z]+)(?P<exp>\d{2}/\d{2}/\d{4})(?P<strike>\d+\.\d{2})(?P<right>[PC])$"
)
# Some PDFs splice EXP into the symbol: LULU06/26/202698.00EXP06/26/26P
OPTION_SYMBOL_EXP_RE = re.compile(
    r"^(?P<ticker>[A-Z]+)(?P<exp>\d{2}/\d{2}/\d{4})(?P<strike>\d+\.\d{2})EXP\d{2}/\d{2}/\d{2}(?P<right>[PC])$"
)
DESC_OPTION_RE = re.compile(
    r"(?P<right>PUT|CALL)(?P<ticker>[A-Z]+).*?\$(?P<strike>\d+(?:\.\d+)?)\s*EXP(?P<exp>\d{2}/\d{2}/\d{2})",
    re.IGNORECASE,
)

PUT_TYPES = {"ROCT PUT", "RULE ONE PUT"}
CALL_TYPES = {"ROCT CALL", "RULE ONE CALL"}


@dataclass(frozen=True)
class Issue:
    check: str
    detail: str
    trade_id: int | None = None


@dataclass(frozen=True)
class EquityMove:
    txn_date: date
    ticker: str
    shares: Decimal
    price: Decimal | None
    kind: str  # purchase / sale


@dataclass(frozen=True)
class OptionFill:
    txn_date: date
    ticker: str
    expiration: date
    strike: Decimal
    right: str  # PUT or CALL
    contracts: Decimal
    premium: Decimal | None
    action: str  # sto / btc / expire / assign_hint


@dataclass
class ExpectedLots:
    account: str
    ticker: str
    expected_shares: int
    tos_lots: list[dict[str, Any]]
    statement_equity_walk: list[dict[str, Any]]


@dataclass
class ReconcileReport:
    issues: list[Issue] = field(default_factory=list)
    statement_equity_shares: Decimal | None = None
    db_shares: Decimal | None = None
    option_fills: int = 0
    okw_trades: int = 0


def load_expected_lots(path: Path) -> ExpectedLots:
    raw = json.loads(path.read_text())
    return ExpectedLots(
        account=raw["account"],
        ticker=raw["ticker"],
        expected_shares=int(raw["expected_shares"]),
        tos_lots=list(raw["tos_lots"]),
        statement_equity_walk=list(raw["statement_equity_walk"]),
    )


def tos_lot_share_total(expected: ExpectedLots) -> int:
    return sum(int(lot["shares"]) for lot in expected.tos_lots)


def statement_walk_share_total(expected: ExpectedLots) -> int:
    return sum(int(row["shares"]) for row in expected.statement_equity_walk)


def check_expected_fixture(expected: ExpectedLots) -> list[Issue]:
    issues: list[Issue] = []
    tos = tos_lot_share_total(expected)
    walk = statement_walk_share_total(expected)
    if tos != expected.expected_shares:
        issues.append(
            Issue(
                "expected_fixture",
                f"TOS lots sum to {tos}, expected_shares={expected.expected_shares}",
            )
        )
    if walk != expected.expected_shares:
        issues.append(
            Issue(
                "expected_fixture",
                f"statement equity walk sums to {walk}, "
                f"expected_shares={expected.expected_shares}",
            )
        )
    return issues


def _parse_mdy(text: str, *, two_digit_year: bool = False) -> date | None:
    try:
        if two_digit_year:
            return datetime.strptime(text, "%m/%d/%y").date()
        return datetime.strptime(text, "%m/%d/%Y").date()
    except ValueError:
        return None


def parse_option_identity(
    symbol_cusip: str | None, description: str
) -> tuple[str, date, Decimal, str] | None:
    """Return (ticker, expiration, strike, PUT|CALL) or None for equity."""
    blob = (symbol_cusip or "").replace(" ", "")
    for pattern in (OPTION_SYMBOL_RE, OPTION_SYMBOL_EXP_RE):
        match = pattern.match(blob)
        if match:
            exp = _parse_mdy(match.group("exp"))
            if exp is None:
                return None
            right = "PUT" if match.group("right") == "P" else "CALL"
            return (
                match.group("ticker"),
                exp,
                Decimal(match.group("strike")),
                right,
            )
    match = DESC_OPTION_RE.search((description or "").replace(" ", ""))
    if not match:
        return None
    exp = _parse_mdy(match.group("exp"), two_digit_year=True)
    if exp is None:
        return None
    raw = match.group("ticker")
    ticker = raw[:4] if len(raw) >= 4 else raw
    return ticker, exp, Decimal(match.group("strike")), match.group("right").upper()


def classify_statement_row(row: TxnRow, ticker: str) -> EquityMove | OptionFill | None:
    ident = parse_option_identity(row.symbol_cusip, row.description)
    category = (row.category or "").strip()
    action = (row.action or "").strip()
    qty = row.quantity if row.quantity is not None else ZERO

    if ident is not None:
        opt_ticker, exp, strike, right = ident
        if opt_ticker != ticker:
            return None
        contracts = abs(qty)
        premium = row.price
        action_l = action.lower()
        cat_l = category.lower()
        if "expired" in action_l or "expired" in cat_l:
            fill_action = "expire"
        elif category == "Purchase" and ("cover" in action_l or "long" in action_l):
            fill_action = "btc"
        elif category == "Purchase":
            fill_action = "btc"
        elif category == "Sale":
            fill_action = "sto"
        elif "option" in action_l or category == "Other":
            fill_action = "assign_hint"
        else:
            return None
        if row.date is None:
            return None
        return OptionFill(
            txn_date=row.date,
            ticker=opt_ticker,
            expiration=exp,
            strike=strike,
            right=right,
            contracts=contracts if contracts != ZERO else Decimal("1"),
            premium=premium,
            action=fill_action,
        )

    symbol = (row.symbol_cusip or "").strip().upper()
    if symbol != ticker:
        return None
    if category not in {"Purchase", "Sale"}:
        return None
    if row.date is None or qty == ZERO:
        return None
    shares = qty if category == "Purchase" else -abs(qty)
    kind = "purchase" if category == "Purchase" else "sale"
    return EquityMove(
        txn_date=row.date,
        ticker=ticker,
        shares=shares,
        price=row.price,
        kind=kind,
    )


def collect_statement_activity(
    statements: list[Any], ticker: str
) -> tuple[list[EquityMove], list[OptionFill]]:
    equity: list[EquityMove] = []
    options: list[OptionFill] = []
    for parsed in statements:
        for row in parsed.transactions:
            classified = classify_statement_row(row, ticker)
            if isinstance(classified, EquityMove):
                equity.append(classified)
            elif isinstance(classified, OptionFill):
                options.append(classified)
    return equity, options


def reconcile_equity_shares(
    equity: list[EquityMove], db_shares: Decimal
) -> list[Issue]:
    issues: list[Issue] = []
    statement_total = sum((m.shares for m in equity), ZERO)
    if statement_total != db_shares:
        issues.append(
            Issue(
                "share_count",
                f"statement equity net {statement_total} vs cost_basis {db_shares}",
            )
        )
    return issues


def _right_matches_type(right: str, trade_type: str) -> bool:
    if right == "PUT":
        return trade_type in PUT_TYPES or "PUT" in trade_type
    return trade_type in CALL_TYPES or "CALL" in trade_type


def reconcile_options(
    fills: list[OptionFill],
    trades: list[Trade],
    events_by_trade: dict[int, list[TradeEvent]],
) -> list[Issue]:
    issues: list[Issue] = []
    unmatched_sto = [f for f in fills if f.action == "sto"]
    used: set[int] = set()

    def _match(fill: OptionFill) -> Trade | None:
        candidates: list[tuple[int, Trade]] = []
        for trade in trades:
            if trade.id in used:
                continue
            if not _right_matches_type(fill.right, trade.trade_type):
                continue
            if trade.expiration_date != fill.expiration:
                continue
            if trade.strike_price is None or abs(trade.strike_price - fill.strike) > PRICE_TOLERANCE:
                continue
            contracts = Decimal(trade.num_of_contracts or 0)
            if contracts != fill.contracts and fill.contracts != ZERO:
                continue
            if trade.date_trade_open is None:
                continue
            delta = abs((trade.date_trade_open - fill.txn_date).days)
            if delta > TRADE_DATE_WINDOW.days:
                continue
            candidates.append((delta, trade))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    for fill in unmatched_sto:
        trade = _match(fill)
        if trade is None:
            issues.append(
                Issue(
                    "missing_okw",
                    f"statement STO {fill.ticker} {fill.right} {fill.strike} "
                    f"exp {fill.expiration} {fill.contracts}ct on {fill.txn_date} "
                    "has no matching OKW trade",
                )
            )
            continue
        used.add(trade.id)
        if fill.premium is not None and trade.credit_debit is not None:
            if abs(trade.credit_debit - fill.premium) > PRICE_TOLERANCE:
                issues.append(
                    Issue(
                        "premium_drift",
                        f"trade {trade.id} credit {trade.credit_debit} vs "
                        f"statement {fill.premium}",
                        trade_id=trade.id,
                    )
                )

    expire_fills = [f for f in fills if f.action == "expire"]
    for fill in expire_fills:
        found = False
        for trade in trades:
            if trade.expiration_date != fill.expiration:
                continue
            long_strike = getattr(trade, "long_strike", None)
            short_hit = (
                _right_matches_type(fill.right, trade.trade_type)
                and trade.strike_price is not None
                and abs(trade.strike_price - fill.strike) <= PRICE_TOLERANCE
            )
            long_hit = (
                "SPREAD" in (trade.trade_type or "").upper()
                and long_strike is not None
                and abs(Decimal(long_strike) - fill.strike) <= PRICE_TOLERANCE
            )
            if not short_hit and not long_hit:
                continue
            evs = events_by_trade.get(trade.id, [])
            if long_hit or any(e.event_type == "EXPIRE" for e in evs):
                found = True
                break
            if any(e.event_type == "ASSIGN" for e in evs):
                issues.append(
                    Issue(
                        "result_mismatch",
                        f"trade {trade.id} is ASSIGN in OKW but statement expired "
                        f"{fill.right} {fill.strike} {fill.expiration}",
                        trade_id=trade.id,
                    )
                )
                found = True
                break
        if not found:
            issues.append(
                Issue(
                    "result_mismatch",
                    f"statement expired {fill.ticker} {fill.right} {fill.strike} "
                    f"exp {fill.expiration} on {fill.txn_date} has no OKW EXPIRE",
                )
            )

    return issues


def db_cost_basis_shares(session: Session, account_name: str, ticker: str) -> Decimal | None:
    account = session.scalar(select(Account).where(Account.account_name == account_name))
    ticker_row = session.scalar(select(Ticker).where(Ticker.ticker == ticker))
    if account is None or ticker_row is None:
        return None
    total = session.scalar(
        select(func.coalesce(func.sum(CostBasis.shares), 0)).where(
            CostBasis.account_id == account.id,
            CostBasis.ticker_id == ticker_row.id,
        )
    )
    return Decimal(total)


def load_okw_trades(
    session: Session, account_name: str, ticker: str
) -> tuple[list[Trade], dict[int, list[TradeEvent]]]:
    account = session.scalar(select(Account).where(Account.account_name == account_name))
    ticker_row = session.scalar(select(Ticker).where(Ticker.ticker == ticker))
    if account is None or ticker_row is None:
        return [], {}
    trades = list(
        session.scalars(
            select(Trade).where(
                Trade.account_id == account.id,
                Trade.ticker_id == ticker_row.id,
                Trade.trade_type.notin_(("BTO", "STC")),
            )
        ).all()
    )
    events_by_trade: dict[int, list[TradeEvent]] = {t.id: [] for t in trades}
    if trades:
        events = session.scalars(
            select(TradeEvent).where(TradeEvent.trade_id.in_([t.id for t in trades]))
        ).all()
        for event in events:
            events_by_trade.setdefault(event.trade_id, []).append(event)
    return trades, events_by_trade


def parse_statement_dir(directory: Path, account_suffix: str) -> list[Any]:
    files = sorted(directory.glob(f"*{account_suffix}*"))
    parsed = []
    for path in files:
        if path.suffix.upper() not in {".PDF", ".pdf"}:
            continue
        name = path.name.upper()
        if "TDA" in name:
            continue
        try:
            parsed.append(parse_statement(path))
        except Exception as exc:  # noqa: BLE001 — keep going across a dirty dump
            print(f"skip {path.name}: {exc}", file=sys.stderr)
    return parsed


def run_reconcile(
    *,
    session: Session | None,
    account_name: str,
    ticker: str,
    statements: list[Any] | None,
    expected: ExpectedLots | None,
) -> ReconcileReport:
    report = ReconcileReport()
    if expected is not None:
        report.issues.extend(check_expected_fixture(expected))
        if session is not None:
            db_shares = db_cost_basis_shares(session, account_name, ticker)
            report.db_shares = db_shares
            if db_shares is None:
                report.issues.append(
                    Issue("share_count", f"no cost_basis for {account_name} {ticker}")
                )
            elif db_shares != Decimal(expected.expected_shares):
                report.issues.append(
                    Issue(
                        "share_count",
                        f"cost_basis {db_shares} vs expected {expected.expected_shares} "
                        f"(TOS lots / statement walk)",
                    )
                )

    if statements:
        equity, options = collect_statement_activity(statements, ticker)
        report.option_fills = len(options)
        report.statement_equity_shares = sum((m.shares for m in equity), ZERO)
        if session is not None:
            db_shares = report.db_shares
            if db_shares is None:
                db_shares = db_cost_basis_shares(session, account_name, ticker)
                report.db_shares = db_shares
            if db_shares is not None:
                report.issues.extend(reconcile_equity_shares(equity, db_shares))
            trades, events_by_trade = load_okw_trades(session, account_name, ticker)
            report.okw_trades = len(trades)
            report.issues.extend(reconcile_options(options, trades, events_by_trade))
        elif expected is not None:
            report.issues.extend(
                reconcile_equity_shares(equity, Decimal(expected.expected_shares))
            )

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--statements-dir", default=None)
    parser.add_argument("--account", default="rule1", choices=sorted(ACCOUNTS))
    parser.add_argument("--ticker", default="LULU")
    parser.add_argument(
        "--expected",
        default=None,
        help="JSON lots fixture (defaults to lulu_rule1_expected_lots.json for LULU).",
    )
    args = parser.parse_args(argv)

    account_name, suffix = ACCOUNTS[args.account]
    ticker = args.ticker.upper()
    expected: ExpectedLots | None = None
    expected_path = Path(args.expected) if args.expected else None
    if expected_path is None and ticker == "LULU" and account_name == "Rule 1":
        expected_path = DEFAULT_LULU_FIXTURE
    if expected_path is not None:
        expected = load_expected_lots(expected_path)

    statements = None
    if args.statements_dir:
        statements = parse_statement_dir(Path(args.statements_dir), suffix)

    session = None
    factory = None
    if args.database_url:
        engine = create_engine(args.database_url)
        factory = sessionmaker(bind=engine)
        session = factory()

    try:
        report = run_reconcile(
            session=session,
            account_name=account_name,
            ticker=ticker,
            statements=statements,
            expected=expected,
        )
    finally:
        if session is not None:
            session.close()

    print(f"ticker={ticker} account={account_name}")
    if report.db_shares is not None:
        print(f"cost_basis shares={report.db_shares}")
    if report.statement_equity_shares is not None:
        print(f"statement equity net={report.statement_equity_shares}")
    print(f"option fills={report.option_fills} okw trades={report.okw_trades}")
    print(f"issues={len(report.issues)}")
    for issue in report.issues:
        suffix_id = f" trade_id={issue.trade_id}" if issue.trade_id else ""
        print(f"  [{issue.check}] {issue.detail}{suffix_id}")
    return 1 if report.issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
