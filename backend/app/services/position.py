"""Per-ticker position summaries: shares owned (split trading vs. long-term),
cost basis, and averages — folded from raw `cost_basis` ledger rows.

Kept decoupled from the ORM/DB on purpose, like `cost_basis.py`/`premium.py`:
callers assemble `PositionEntry` tuples from `cost_basis` rows joined back to
the originating trade's type name (for classification) and hand them to
`summarize_positions` here, so this stays trivially unit-testable.

Trading vs. long-term classification (see AGENTS.md): `ROCT *`/`ROCS *`
trade types are the trading-shares program, `RULE ONE *` is the long-term
investment-shares program. Plain stock trades (`BTO`/`STC`) have no
options-program prefix to key off of, so they default to `long_term` — a
deliberate simplification (see the ticker drill-down plan's flagged
assumptions), not a fact this module can derive from the data alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Sequence

ZERO = Decimal("0")

ShareClass = Literal["trading", "long_term"]

# (share_class, shares, total_amount) — one row per `cost_basis` entry, already
# classified by the caller (see `classify_share_class`).
PositionEntry = tuple[ShareClass, Decimal, Decimal]


def classify_share_class(trade_type_name: str | None) -> ShareClass:
    """Maps a trade type name to the trading-vs-long-term share bucket.

    `None` (a `cost_basis` row with no linked trade, e.g. a manual cash-flow
    adjustment) also defaults to `long_term`, for the same reason `BTO`/`STC`
    do — there's no options-program prefix to classify by.
    """
    if trade_type_name is None:
        return "long_term"
    name = trade_type_name.upper()
    if name.startswith("ROCT") or name.startswith("ROCS"):
        return "trading"
    return "long_term"


@dataclass(frozen=True)
class ClassTotals:
    shares: Decimal
    cost_basis: Decimal
    avg_cost_per_share: Decimal | None


@dataclass(frozen=True)
class PositionTotals:
    total: ClassTotals
    trading: ClassTotals
    long_term: ClassTotals


def _fold(entries: Sequence[tuple[Decimal, Decimal]]) -> ClassTotals:
    shares = sum((s for s, _ in entries), start=ZERO)
    cost_basis = sum((a for _, a in entries), start=ZERO)
    avg_cost_per_share = cost_basis / shares if shares != ZERO else None
    return ClassTotals(shares=shares, cost_basis=cost_basis, avg_cost_per_share=avg_cost_per_share)


def reduced_cost_basis(acquisition_cost: Decimal, premium_collected: Decimal) -> Decimal:
    """The spreadsheet's running BASIS column: purchase/assignment dollars
    minus premiums collected (and sale proceeds, which already live in
    `acquisition_cost` as negative `cost_basis` rows).

    Matches `Rule One Basis Reduction spreadsheet.xlsx`: Amount is +cost
    on a buy/assign and −premium on a sold option; BASIS is the running
    sum; Basis/sh = BASIS / shares held.
    """
    return acquisition_cost - premium_collected


def basis_per_share(
    acquisition_cost: Decimal, premium_collected: Decimal, shares: Decimal
) -> Decimal | None:
    """Spreadsheet `Basis/sh` = (costs − premiums) / shares. `None` when
    no shares are held (the sheet shows `#DIV/0!`)."""
    if shares == ZERO:
        return None
    return reduced_cost_basis(acquisition_cost, premium_collected) / shares


def fold_amount_ledger(
    rows: Sequence[tuple[Decimal, Decimal]],
) -> tuple[Decimal, Decimal, Decimal | None]:
    """Replay the spreadsheet Amount ledger: each row is
    `(share_delta, amount)` where amount is already signed."""
    shares = sum((s for s, _ in rows), start=ZERO)
    basis = sum((a for _, a in rows), start=ZERO)
    per_share = basis / shares if shares != ZERO else None
    return shares, basis, per_share


def summarize_positions(entries: Sequence[PositionEntry]) -> PositionTotals:
    """Folds classified `cost_basis` rows into total/trading/long-term
    share counts, cost basis, and average cost per share.

    Division by zero (no shares currently held in a bucket) is guarded by
    returning `None` for that bucket's `avg_cost_per_share`, matching
    `cost_basis.calculate_cost_basis`'s convention.
    """
    trading_rows = [(s, a) for cls, s, a in entries if cls == "trading"]
    long_term_rows = [(s, a) for cls, s, a in entries if cls == "long_term"]
    all_rows = [(s, a) for _, s, a in entries]
    return PositionTotals(
        total=_fold(all_rows),
        trading=_fold(trading_rows),
        long_term=_fold(long_term_rows),
    )
