"""Read-only import validation — the promotion gate before staging/prod.

Recomputes money math from stored inputs using `app.services.*`, walks
`cost_basis` the same way `v_cost_basis_running` does, and flags structural
problems (needs_review leftovers, duplicate option keys, orphan FKs).
Optional `--expected` JSON reconciles per-account-year premium totals and
year-start bankroll.

Talks to a DATABASE_URL you supply. Never writes.

    python -m scripts.validate_import --database-url ... [--expected expected.json]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Account, Bankroll, CashFlow, CostBasis, Ticker, Trade, TradeType
from app.services.cost_basis import calculate_cost_basis
from app.services.premium import premium_for_trade
from app.services.rorc import calculate_arorc, calculate_rorc

ZERO = Decimal("0")
HUNDRED = Decimal("100")
ARORC_TOLERANCE = Decimal("0.0001")  # 1 bp of the ratio; column is NUMERIC(10,6)
ARORC_RELATIVE_TOLERANCE = Decimal("0.001")  # 10 bp relative; covers stored vs Decimal recompute
MONEY_TOLERANCE = Decimal("0.01")
NET_CREDIT_TOLERANCE = Decimal("0.000001")
# Adjacent running basis/share jumps larger than this (and > $10 absolute)
# are treated as implausible — first row of a ticker is exempt.
BASIS_RELATIVE_SWING = Decimal("0.5")
BASIS_ABSOLUTE_SWING = Decimal("10")
PUT_LIKE = {"ROCT PUT", "RULE ONE PUT", "ROCS BULL PUT SPREAD", "BULL PUT SPREAD"}
OPTIONS_CREDIT_SKIP_TYPES = {"BTO", "STC"}


@dataclass(frozen=True)
class Issue:
    check: str
    detail: str
    trade_id: int | None = None


@dataclass
class PremiumTotal:
    account_name: str
    year: int
    total: Decimal


@dataclass
class BankrollYearStart:
    account_name: str
    year: int
    balance: Decimal


@dataclass
class ExpectedTotals:
    premium: list[PremiumTotal] = field(default_factory=list)
    bankroll_year_start: list[BankrollYearStart] = field(default_factory=list)


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)
    premium_by_account_year: dict[tuple[str, int], Decimal] = field(default_factory=dict)
    trades_checked: int = 0
    cost_basis_rows: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues


def _dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _as_percent(margin_percent: Decimal | None) -> Decimal:
    """OKW stores 1.00 as full cash (ratio); `calculate_rorc` takes 0–100."""
    if margin_percent is None or margin_percent == ZERO:
        return HUNDRED
    if margin_percent <= 1:
        return margin_percent * HUNDRED
    return margin_percent


def load_expected(path: Path) -> ExpectedTotals:
    payload = json.loads(path.read_text())
    expected = ExpectedTotals()
    for row in payload.get("premium") or []:
        expected.premium.append(
            PremiumTotal(
                account_name=row["account"],
                year=int(row["year"]),
                total=_dec(row["total"]),
            )
        )
    for row in payload.get("bankroll_year_start") or []:
        expected.bankroll_year_start.append(
            BankrollYearStart(
                account_name=row["account"],
                year=int(row["year"]),
                balance=_dec(row["balance"]),
            )
        )
    return expected


def expected_net_credit(trade: Trade) -> Decimal:
    return _dec(trade.credit_debit) - _dec(trade.commission_per_share)


def expected_risk_capital(trade: Trade, type_name: str) -> Decimal | None:
    if type_name not in PUT_LIKE:
        return None
    strike = trade.strike_price
    if strike is None:
        return None
    net = trade.net_credit_per_share
    if net is None:
        net = expected_net_credit(trade)
    width = _dec(strike)
    if trade.long_strike is not None:
        width = _dec(strike) - _dec(trade.long_strike)
    return width - _dec(net)


def check_trade_math(trade: Trade, type_name: str, category: str) -> list[Issue]:
    """Recompute NC / RC / ARORC from stored inputs. Skips plain stock."""
    if type_name in OPTIONS_CREDIT_SKIP_TYPES or category != "OPTIONS":
        return []
    issues: list[Issue] = []
    recomputed_nc = expected_net_credit(trade)
    if trade.net_credit_per_share is not None:
        if abs(_dec(trade.net_credit_per_share) - recomputed_nc) > NET_CREDIT_TOLERANCE:
            issues.append(
                Issue(
                    "trade_math",
                    (
                        f"net_credit_per_share stored {trade.net_credit_per_share} "
                        f"vs credit_debit - commission {recomputed_nc}"
                    ),
                    trade_id=trade.id,
                )
            )
    if trade.net_credit_per_share is not None:
        nc = _dec(trade.net_credit_per_share)
    else:
        nc = recomputed_nc

    recomputed_rc = expected_risk_capital(trade, type_name)
    if recomputed_rc is not None and trade.risk_capital_per_share is not None:
        if abs(_dec(trade.risk_capital_per_share) - recomputed_rc) > MONEY_TOLERANCE:
            issues.append(
                Issue(
                    "trade_math",
                    (
                        f"risk_capital_per_share stored {trade.risk_capital_per_share} "
                        f"vs recomputed {recomputed_rc}"
                    ),
                    trade_id=trade.id,
                )
            )
    rc = (
        _dec(trade.risk_capital_per_share)
        if trade.risk_capital_per_share is not None
        else recomputed_rc
    )
    if rc is None or trade.days_to_expiration is None:
        return issues

    rorc = calculate_rorc(nc, rc, _as_percent(trade.margin_percent))
    recomputed_arorc = calculate_arorc(rorc, trade.days_to_expiration)
    if trade.arorc is not None:
        stored = _dec(trade.arorc).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        recomputed = recomputed_arorc.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        allowed = max(ARORC_TOLERANCE, abs(recomputed) * ARORC_RELATIVE_TOLERANCE)
        if abs(stored - recomputed) > allowed:
            issues.append(
                Issue(
                    "trade_math",
                    f"arorc stored {trade.arorc} vs recomputed {recomputed_arorc}",
                    trade_id=trade.id,
                )
            )
    return issues


def walk_cost_basis(rows: list[CostBasis]) -> list[Issue]:
    """Fold rows in view order; flag negative shares and implausible BPS jumps."""
    issues: list[Issue] = []
    grouped: dict[tuple[int, int], list[CostBasis]] = defaultdict(list)
    for row in rows:
        grouped[(row.account_id, row.ticker_id)].append(row)

    for (_account_id, _ticker_id), lot in grouped.items():
        lot.sort(key=lambda r: (r.transaction_date, r.id))
        prior_basis = ZERO
        prior_shares = ZERO
        prior_bps: Decimal | None = None
        for index, row in enumerate(lot):
            running = calculate_cost_basis(
                prior_basis,
                prior_shares,
                _dec(row.total_amount),
                Decimal(row.shares),
            )
            if running.running_shares < ZERO:
                issues.append(
                    Issue(
                        "cost_basis",
                        (
                            f"negative running_shares {running.running_shares} after "
                            f"cost_basis id={row.id} ticker_id={row.ticker_id}"
                        ),
                        trade_id=row.trade_id,
                    )
                )
            elif (
                index > 0
                and prior_shares > ZERO
                and prior_bps is not None
                and running.basis_per_share is not None
                and row.shares > 0
                and _dec(row.total_amount) != ZERO
                # Call-aways reduce basis by strike proceeds, so leftover BPS is
                # not economic cost. A later ASSIGN at strike then looks like a
                # huge jump even when the statement inventory is correct.
                and not (row.description or "").startswith("Assigned")
            ):
                delta = abs(running.basis_per_share - prior_bps)
                scale = max(abs(prior_bps), Decimal("1"))
                if delta > BASIS_ABSOLUTE_SWING and (delta / scale) > BASIS_RELATIVE_SWING:
                    issues.append(
                        Issue(
                            "cost_basis",
                            (
                                f"implausible basis_per_share swing {prior_bps} → "
                                f"{running.basis_per_share} at cost_basis id={row.id}"
                            ),
                            trade_id=row.trade_id,
                        )
                    )
            prior_basis = running.running_basis
            prior_shares = running.running_shares
            prior_bps = running.basis_per_share
    return issues


def duplicate_option_keys(trades: list[Trade], type_by_id: dict[int, TradeType]) -> list[Issue]:
    """Same account/ticker/type/strike/expiration/open date/contracts, excluding rolls."""
    buckets: dict[tuple, list[Trade]] = defaultdict(list)
    for trade in trades:
        type_row = type_by_id.get(trade.trade_type_id)
        if type_row is None or type_row.category != "OPTIONS":
            continue
        if trade.trade_parent_id is not None:
            continue
        key = (
            trade.account_id,
            trade.ticker_id,
            trade.trade_type,
            trade.strike_price,
            trade.expiration_date,
            trade.date_trade_open,
            trade.num_of_contracts,
            trade.credit_debit,
        )
        buckets[key].append(trade)
    issues: list[Issue] = []
    for _key, group in buckets.items():
        if len(group) < 2:
            continue
        ids = [t.id for t in group]
        issues.append(
            Issue(
                "duplicates",
                (f"duplicate (account, ticker, strike, expiration, open date) trade ids {ids}"),
                trade_id=ids[0],
            )
        )
    return issues


def orphan_issues(
    *,
    trade_ids: set[int],
    ticker_ids: set[int],
    cash_flows: list[CashFlow],
    cost_basis: list[CostBasis],
) -> list[Issue]:
    issues: list[Issue] = []
    for row in cash_flows:
        if row.trade_id is not None and row.trade_id not in trade_ids:
            issues.append(
                Issue("orphans", f"cash_flows id={row.id} trade_id={row.trade_id} missing")
            )
        if row.ticker_id is not None and row.ticker_id not in ticker_ids:
            issues.append(
                Issue("orphans", f"cash_flows id={row.id} ticker_id={row.ticker_id} missing")
            )
    for row in cost_basis:
        if row.trade_id is not None and row.trade_id not in trade_ids:
            issues.append(
                Issue(
                    "orphans",
                    f"cost_basis id={row.id} trade_id={row.trade_id} missing",
                    trade_id=row.trade_id,
                )
            )
        if row.ticker_id not in ticker_ids:
            issues.append(
                Issue("orphans", f"cost_basis id={row.id} ticker_id={row.ticker_id} missing")
            )
    return issues


def reconcile_premium(
    computed: dict[tuple[str, int], Decimal],
    expected: list[PremiumTotal],
) -> list[Issue]:
    issues: list[Issue] = []
    for row in expected:
        key = (row.account_name, row.year)
        actual = computed.get(key, ZERO)
        if abs(actual - row.total) > MONEY_TOLERANCE:
            issues.append(
                Issue(
                    "premium",
                    (f"{row.account_name} {row.year}: premium {actual} vs expected {row.total}"),
                )
            )
    return issues


def running_bankroll_before(
    *,
    starting_balance: Decimal,
    rows: list[Bankroll],
    as_of: date,
) -> Decimal:
    total = _dec(starting_balance)
    for row in rows:
        if row.transaction_date < as_of:
            total += _dec(row.transaction_amount)
    return total


def reconcile_bankroll(
    *,
    accounts: dict[str, Account],
    bankroll_by_account: dict[int, list[Bankroll]],
    expected: list[BankrollYearStart],
) -> list[Issue]:
    issues: list[Issue] = []
    if not expected:
        return issues
    any_rows = any(bankroll_by_account.values())
    if not any_rows:
        issues.append(
            Issue(
                "bankroll",
                "expected bankroll_year_start provided but bankroll table is empty",
            )
        )
        return issues
    for row in expected:
        account = accounts.get(row.account_name)
        if account is None:
            issues.append(Issue("bankroll", f"unknown account {row.account_name!r}"))
            continue
        as_of = date(row.year, 1, 1)
        actual = running_bankroll_before(
            starting_balance=_dec(account.starting_balance),
            rows=bankroll_by_account.get(account.id, []),
            as_of=as_of,
        )
        if abs(actual - row.balance) > MONEY_TOLERANCE:
            issues.append(
                Issue(
                    "bankroll",
                    (
                        f"{row.account_name} year-start {row.year}: running {actual} "
                        f"vs expected {row.balance}"
                    ),
                )
            )
    return issues


def run_validation(session: Session, expected: ExpectedTotals | None = None) -> ValidationReport:
    report = ValidationReport()
    accounts = {a.account_name: a for a in session.scalars(select(Account)).all()}
    accounts_by_id = {a.id: a for a in accounts.values()}
    tickers = session.scalars(select(Ticker)).all()
    ticker_ids = {t.id for t in tickers}
    type_rows = session.scalars(select(TradeType)).all()
    type_by_id = {t.id: t for t in type_rows}
    trades = session.scalars(select(Trade).order_by(Trade.id)).all()
    cash_flows = session.scalars(select(CashFlow)).all()
    cost_rows = session.scalars(select(CostBasis)).all()
    bankroll_rows = session.scalars(select(Bankroll)).all()

    report.trades_checked = len(trades)
    report.cost_basis_rows = len(cost_rows)

    for trade in trades:
        if trade.needs_review:
            report.issues.append(
                Issue("needs_review", "trade still flagged needs_review", trade_id=trade.id)
            )
        type_row = type_by_id.get(trade.trade_type_id)
        if type_row is None:
            report.issues.append(
                Issue("orphans", f"trade id={trade.id} trade_type_id missing", trade_id=trade.id)
            )
            continue
        report.issues.extend(check_trade_math(trade, type_row.type_name, type_row.category))
        premium = premium_for_trade(
            trade_type_category=type_row.category,
            is_credit=type_row.is_credit,
            net_credit_per_share=trade.net_credit_per_share,
            num_of_contracts=trade.num_of_contracts,
        )
        if premium is not None:
            account = accounts_by_id.get(trade.account_id)
            if account is not None:
                key = (account.account_name, trade.date_trade_open.year)
                report.premium_by_account_year[key] = (
                    report.premium_by_account_year.get(key, ZERO) + premium
                )

    report.issues.extend(walk_cost_basis(list(cost_rows)))
    report.issues.extend(duplicate_option_keys(list(trades), type_by_id))
    report.issues.extend(
        orphan_issues(
            trade_ids={t.id for t in trades},
            ticker_ids=ticker_ids,
            cash_flows=list(cash_flows),
            cost_basis=list(cost_rows),
        )
    )

    bankroll_by_account: dict[int, list[Bankroll]] = defaultdict(list)
    for row in bankroll_rows:
        bankroll_by_account[row.account_id].append(row)

    if expected is not None:
        report.issues.extend(reconcile_premium(report.premium_by_account_year, expected.premium))
        report.issues.extend(
            reconcile_bankroll(
                accounts=accounts,
                bankroll_by_account=bankroll_by_account,
                expected=expected.bankroll_year_start,
            )
        )
    return report


def _print_report(report: ValidationReport) -> None:
    print(f"trades checked: {report.trades_checked}")
    print(f"cost_basis rows: {report.cost_basis_rows}")
    if report.premium_by_account_year:
        print("premium totals (computed):")
        for (account, year), total in sorted(report.premium_by_account_year.items()):
            print(f"  {account} {year}: {total}")
    if report.ok:
        print("validation: clean")
        return
    print(f"validation: {len(report.issues)} issue(s)")
    for issue in report.issues:
        suffix = f" trade_id={issue.trade_id}" if issue.trade_id is not None else ""
        print(f"  [{issue.check}] {issue.detail}{suffix}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--database-url", required=True, help="Target DATABASE_URL — read only.")
    parser.add_argument(
        "--expected",
        default=None,
        help="JSON with optional premium[] and bankroll_year_start[] expected totals.",
    )
    args = parser.parse_args(argv)

    expected = load_expected(Path(args.expected)) if args.expected else None
    engine = create_engine(args.database_url)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        report = run_validation(session, expected)
    _print_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
