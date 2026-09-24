"""Roll chains: OKW shows adjacent columns as one continuing trade; the DB
keeps one `trades` row per column and links them with `trade_parent_id`.

Pure helpers — callers supply already-fetched rows. Used by the OKW loader
(adjacent columns), `link_roll_parents` (historical backfill), and the
trades list API (collapse a chain to one display row).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Sequence

from app.services.rorc import calculate_arorc, calculate_rorc

# Continuation opened more than this after the ROLL event is treated as a
# new independent trade, not the next OKW column.
_ROLL_OPEN_SLACK = timedelta(days=5)


@dataclass(frozen=True)
class RollLinkRow:
    id: int
    account_id: int
    ticker: str
    trade_type: str
    num_of_contracts: int | None
    date_trade_open: date
    expiration_date: date | None
    strike_price: Decimal | None
    trade_parent_id: int | None
    latest_event: str | None
    roll_date: date | None


@dataclass(frozen=True)
class ChainSpec:
    """Root-to-tip trade ids for one roll chain (a single-leg trade is a
    chain of length 1)."""

    ids: tuple[int, ...]

    @property
    def root_id(self) -> int:
        return self.ids[0]

    @property
    def tip_id(self) -> int:
        return self.ids[-1]


def format_strike_path(strikes: Sequence[Decimal | None]) -> str | None:
    """`347.50 → 342.50 → 335.00`, or None when there's nothing to show."""
    parts: list[str] = []
    for strike in strikes:
        if strike is None:
            continue
        parts.append(f"{strike:.2f}")
    if len(parts) <= 1:
        return None
    return " → ".join(parts)


def option_right(trade_type: str | None) -> str | None:
    """PUT vs CALL from `trades.trade_type` (e.g. ROCT PUT, RULE ONE CALL)."""
    if not trade_type:
        return None
    name = trade_type.upper()
    if "PUT" in name:
        return "PUT"
    if "CALL" in name:
        return "CALL"
    return None


def classify_roll(
    *,
    from_strike: Decimal | None,
    from_expiration: date | None,
    to_strike: Decimal | None,
    to_expiration: date | None,
) -> str:
    """Schwab / thinkorswim roll names:

    - vertical: new strike, same expiration
    - calendar: same strike, new expiration
    - diagonal: new strike and new expiration (RBLX $36 31 JUL → $34.50 7 AUG)
    """
    strike_changed = (
        from_strike is not None and to_strike is not None and from_strike != to_strike
    )
    exp_changed = (
        from_expiration is not None
        and to_expiration is not None
        and from_expiration != to_expiration
    )
    if strike_changed and exp_changed:
        return "diagonal"
    if strike_changed:
        return "vertical"
    if exp_changed:
        return "calendar"
    return "vertical"


def chain_arorc(
    *,
    date_trade_open: date,
    expiration_date: date | None,
    net_credits: Sequence[Decimal | None],
    risk_capital_per_share: Decimal | None,
    margin_percent: Decimal | None,
) -> Decimal | None:
    """Treat the OKW columns as one position: sum net credit, original RC,
    calendar days from first open to last expiration, then the usual
    ARORC = (NC/RC) * (365/days).
    """
    credits = [credit for credit in net_credits if credit is not None]
    if not credits or risk_capital_per_share is None or expiration_date is None:
        return None
    total_nc = sum(credits, start=Decimal("0"))
    days = (expiration_date - date_trade_open).days
    margin = margin_percent if margin_percent is not None else Decimal("100")
    # OKW stores 100% cash-secured as 1 (and 50% as 0.5), not 100.
    if Decimal("0") < margin <= Decimal("1"):
        margin = margin * Decimal("100")
    result = calculate_arorc(
        calculate_rorc(total_nc, risk_capital_per_share, margin), days
    )
    return result.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def build_chains(parent_of: dict[int, int | None]) -> list[ChainSpec]:
    """`parent_of` maps trade id → trade_parent_id (None if root).

    Continuation legs are omitted as their own chains. If a parent id is
    missing from the map (filtered out of the current result set), the
    child becomes a root so it still displays.
    """
    known = set(parent_of)
    children: dict[int, list[int]] = {}
    for trade_id, parent_id in parent_of.items():
        if parent_id is not None and parent_id in known:
            children.setdefault(parent_id, []).append(trade_id)
    for child_ids in children.values():
        child_ids.sort()

    child_ids = {cid for ids in children.values() for cid in ids}
    roots = sorted(tid for tid in parent_of if tid not in child_ids)

    chains: list[ChainSpec] = []
    for root_id in roots:
        ids = [root_id]
        seen = {root_id}
        while True:
            nxt = children.get(ids[-1])
            if not nxt:
                break
            child_id = nxt[0]
            if child_id in seen:
                break
            ids.append(child_id)
            seen.add(child_id)
        chains.append(ChainSpec(tuple(ids)))
    return chains


def _contracts_key(n: int | None) -> int:
    return n or 0


def _strike_distance(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    if a is None or b is None:
        return None
    return abs(a - b)


def _pick_continuation(parent: RollLinkRow, candidates: list[RollLinkRow]) -> RollLinkRow | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    if parent.strike_price is None:
        return None
    scored: list[tuple[Decimal, Decimal, int, RollLinkRow]] = []
    for row in candidates:
        strike_dist = _strike_distance(parent.strike_price, row.strike_price)
        if strike_dist is None:
            continue
        if parent.expiration_date is not None and row.expiration_date is not None:
            exp_dist = Decimal(abs((row.expiration_date - parent.expiration_date).days))
        else:
            exp_dist = Decimal("9999")
        scored.append((strike_dist, exp_dist, row.id, row))
    if not scored:
        return None
    scored.sort()
    return scored[0][3]


def match_roll_parents(rows: Sequence[RollLinkRow]) -> list[tuple[int, int]]:
    """Pair unmatched ROLL tips with the next OKW column.

    Returns `(child_id, parent_id)` links. Already-parented children are
    left alone. Each parent is identified by its *original* strike; the
    continuation is the unused same-day open (new strike is expected). When
    two parents roll to the same new strike on the same day, the nearer new
    expiration is paired first (RBLX $36 → 7 AUG, $36.50 → 14 AUG).
    """
    claimed_children = {row.id for row in rows if row.trade_parent_id is not None}
    has_child = {row.trade_parent_id for row in rows if row.trade_parent_id is not None}

    parents = [
        row
        for row in rows
        if row.latest_event == "ROLL"
        and row.roll_date is not None
        and row.id not in has_child
    ]
    parents.sort(key=lambda row: (row.roll_date or row.date_trade_open, row.id))

    links: list[tuple[int, int]] = []
    by_id = {row.id: row for row in rows}

    def same_line(parent: RollLinkRow, child: RollLinkRow) -> bool:
        return (
            child.account_id == parent.account_id
            and child.ticker == parent.ticker
            and child.trade_type == parent.trade_type
            and _contracts_key(child.num_of_contracts) == _contracts_key(parent.num_of_contracts)
            and child.date_trade_open >= parent.date_trade_open
        )

    for parent in parents:
        unused = [
            row
            for row in rows
            if row.id != parent.id
            and row.id not in claimed_children
            and row.trade_parent_id is None
            and same_line(parent, row)
        ]
        exact = [row for row in unused if row.date_trade_open == parent.roll_date]
        chosen = _pick_continuation(parent, exact)
        if chosen is None:
            slack = [
                row
                for row in unused
                if parent.roll_date <= row.date_trade_open <= parent.roll_date + _ROLL_OPEN_SLACK
            ]
            chosen = _pick_continuation(parent, slack)
        if chosen is None:
            continue
        # Don't attach a trade that is itself an earlier parent in this batch
        # in a way that would cycle (child already an ancestor).
        ancestor = parent.id
        cycle = False
        seen: set[int] = set()
        while ancestor is not None and ancestor not in seen:
            seen.add(ancestor)
            if ancestor == chosen.id:
                cycle = True
                break
            node = by_id.get(ancestor)
            ancestor = node.trade_parent_id if node else None
        if cycle:
            continue
        links.append((chosen.id, parent.id))
        claimed_children.add(chosen.id)
        by_id[chosen.id] = replace(chosen, trade_parent_id=parent.id)
    return links
