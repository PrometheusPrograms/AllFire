"""Apply reviewed statement-truth corrections to OKW-imported option trades.

Rule: **Schwab statements are ground truth for fills.** OKW is the strategy log
and is sometimes wrong — a quoted limit price recorded instead of the fill, a
contract count that never filled, a mistyped expiration, or the old fake
Saturday expiration used to dodge 0-DTE in the ARORC formula (the app now
annualizes same-day trades as 1 day; see `app.services.rorc`).

Each correction in `scripts/data/statement_corrections.json` names the
statement evidence and matches trades by natural key (account, ticker, type,
open date, expiration, strike, plus optional discriminators) — never a DB id —
so the same file applies to dev, staging and production after the OKW import.
Run it right after `scripts.import_historical` and before
`scripts.backfill_cost_basis` (see PRODUCTION_IMPORT_RUNBOOK.md).

Applying a correction:
- overwrites the listed trade fields (a fix to what was imported, not a
  lifecycle change — lifecycle stays in trade_events), including identity
  typos: `ticker` (GLTB -> GTLB), `trade_type` (a CALL that was a PUT) and
  `account` (booked in Rule 1, filled in Roth) — their ids follow, and any
  share lot moves with them;
- with `"recompute": true`, rederives net credit, risk capital, margin capital,
  DTE, ARORC (and the expired result/final ARORC) from the corrected fill with
  the same formulas as the spreadsheet (`app.services.rorc`);
- sets or adds closing events (`"events": {"EXPIRE": "2026-08-28"}`);
- sets a closing event's debit fields (`"event_fields": {"ROLL": {"closing_debit":
  null, "total_debit": "0"}}`) — e.g. clearing an OKW roll's closing debit once
  that roll's net is stored as the next leg's credit, so it counts once;
- changes an outcome the statement contradicts
  (`"outcome": {"from": "ASSIGN", "to": "EXPIRE", "date": "2025-01-10"}`);
- adds a trade the statements (or, before the month's statement exists, the
  Schwab API) show but OKW never recorded (`"add": {"num_of_contracts": 2,
  "credit_debit": "0.35", "commission_per_share": "0.0041"}`) — keyed by the
  entry's natural key, so a re-run finds it and changes nothing;
- links a roll continuation to the leg it rolled from
  (`"parent": {"ticker": …, "trade_type": …, "date_trade_open": …,
  "expiration_date": …, "strike_price": …}`, same account);
- splits a partial assignment into an assigned trade and an expired sibling
  (`"split": {"assigned_contracts": 1, "expired_contracts": 1, "expire_date": …}`);
- removes a row the statements show never filled, or a duplicate of another
  row (`"remove": true`) — backed up to JSON first;
- rebuilds the share lot (`cost_basis`) of every option trade it touched, with
  the same math as `scripts.backfill_cost_basis`;
- appends an audit line to `trades.notes`.

Idempotent: a correction whose `match` no longer finds a trade but whose
corrected state does (or, for `remove`, that finds nothing) is reported as
already applied.

    python -m scripts.apply_statement_corrections --database-url ... [--dry-run] [--file ...]
        [--backup removed_by_statement_corrections.json]
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.models import Account, CashFlow, CostBasis, Trade, TradeEvent
from app.services.rorc import calculate_arorc, calculate_rorc, effective_days_to_expiration
from scripts.backfill_cost_basis import (
    CALL_LIKE_ASSIGNABLE,
    PUT_LIKE_ASSIGNABLE,
    assignment_lot,
)
from app.services.premium import total_debit_for, trade_shares
from scripts.okw_loader import get_or_create_ticker, get_trade_type

DEFAULT_FILE = Path(__file__).with_name("data") / "statement_corrections.json"
EVENT_BATCH = "stmt_corrections"
CONTRACT_MULTIPLIER = Decimal("100")
HUNDRED = Decimal("100")

DATE_FIELDS = {"date_trade_open", "expiration_date"}
INT_FIELDS = {"num_of_contracts", "num_of_shares", "days_to_expiration"}
KEY_FIELDS = ("ticker", "trade_type", "date_trade_open", "expiration_date", "strike_price")


@dataclass
class CorrectionResult:
    applied: list[str] = field(default_factory=list)
    already_applied: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)  # backup of deleted rows


def _as_dict(row) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _remove_trade(session: Session, trade: Trade) -> dict[str, Any]:
    """Delete a trade with its events and lots; return a backup of all of it.
    Roll-chain children and cash flows pointing at it are unlinked, not deleted."""
    backup = {
        "trade": _as_dict(trade),
        "trade_events": [
            _as_dict(e)
            for e in session.scalars(select(TradeEvent).where(TradeEvent.trade_id == trade.id))
        ],
        "cost_basis": [
            _as_dict(c)
            for c in session.scalars(select(CostBasis).where(CostBasis.trade_id == trade.id))
        ],
        "unlinked_children": list(
            session.scalars(select(Trade.id).where(Trade.trade_parent_id == trade.id))
        ),
    }
    session.execute(
        update(Trade).where(Trade.trade_parent_id == trade.id).values(trade_parent_id=None)
    )
    session.execute(update(CashFlow).where(CashFlow.trade_id == trade.id).values(trade_id=None))
    session.execute(delete(CostBasis).where(CostBasis.trade_id == trade.id))
    session.execute(delete(TradeEvent).where(TradeEvent.trade_id == trade.id))
    session.delete(trade)
    return backup


def _rebuild_lot(session: Session, trade: Trade) -> str | None:
    """Make the trade's share lot match its (corrected) outcome."""
    if trade.trade_type not in PUT_LIKE_ASSIGNABLE | CALL_LIKE_ASSIGNABLE:
        return None
    lots = list(session.scalars(select(CostBasis).where(CostBasis.trade_id == trade.id)))
    before = sorted(
        (lot.shares, lot.cost_per_share, lot.transaction_date, lot.ticker_id, lot.account_id)
        for lot in lots
    )
    assign = session.scalar(
        select(TradeEvent)
        .where(TradeEvent.trade_id == trade.id, TradeEvent.event_type == "ASSIGN")
        .order_by(TradeEvent.event_date.desc(), TradeEvent.id.desc())
        .limit(1)
    )
    wanted = (
        assignment_lot(trade, trade.trade_type, assign)
        if assign is not None and _closing_outcome(session, trade) == "ASSIGN"
        else None
    )
    after = (
        []
        if wanted is None
        else [
            (
                wanted.shares,
                wanted.cost_per_share,
                wanted.transaction_date,
                wanted.ticker_id,
                wanted.account_id,
            )
        ]
    )
    if before == after:
        return None
    for lot in lots:
        session.delete(lot)
    if wanted is not None:
        session.add(wanted)
        return (
            f"share lot -> {wanted.shares} sh @ {wanted.cost_per_share} "
            f"on {wanted.transaction_date}"
        )
    return "share lot removed"


def _change_outcome(session: Session, trade: Trade, spec: dict[str, str]) -> str | None:
    """Swap an ASSIGN/EXPIRE event the statement contradicts."""
    on = date.fromisoformat(spec["date"])
    wrong = session.scalar(
        select(TradeEvent).where(
            TradeEvent.trade_id == trade.id, TradeEvent.event_type == spec["from"]
        )
    )
    if wrong is None:
        return None
    wrong.event_type, old_date, wrong.event_date = spec["to"], wrong.event_date, on
    return f"{spec['from']} {old_date} -> {spec['to']} {on}"


def _split_off_expired(
    session: Session, trade: Trade, spec: dict[str, Any], *, today: date, reason: str
) -> tuple[Trade, str]:
    """Partial assignment: keep the assigned contracts on `trade`, move the rest
    to a new sibling trade that expired."""
    assigned, expired = int(spec["assigned_contracts"]), int(spec["expired_contracts"])
    sibling = Trade(
        **{
            name: value
            for name, value in _as_dict(trade).items()
            if name not in {"id", "created_at", "notes", "import_batch", "trade_parent_id"}
        }
    )
    sibling.num_of_contracts, sibling.num_of_shares = expired, expired * 100
    sibling.import_batch = EVENT_BATCH
    sibling.notes = (
        f"[statement truth {today}] {reason} (split from #{trade.id}: "
        f"{expired} of {assigned + expired} contracts expired)"
    )
    trade.num_of_contracts, trade.num_of_shares = assigned, assigned * 100
    for row in (trade, sibling):
        recompute_fill_fields(row)
    session.add(sibling)
    session.flush()
    session.add(
        TradeEvent(
            trade_id=sibling.id,
            event_type="OPEN",
            event_date=sibling.date_trade_open,
            import_batch=EVENT_BATCH,
        )
    )
    session.add(
        TradeEvent(
            trade_id=sibling.id,
            event_type="EXPIRE",
            event_date=date.fromisoformat(spec["expire_date"]),
            import_batch=EVENT_BATCH,
        )
    )
    sibling.result_net_credit = (
        sibling.net_credit_per_share * CONTRACT_MULTIPLIER * Decimal(expired)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return (
        sibling,
        f"split: {assigned} contract(s) assigned here, {expired} expired -> new #{sibling.id}",
    )


def _coerce(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name in DATE_FIELDS:
        return date.fromisoformat(value)
    if name in INT_FIELDS:
        return int(value)
    if isinstance(value, (int, float, str)) and name not in {"ticker", "trade_type", "notes"}:
        return Decimal(str(value))
    return value


def _find(session: Session, account_id: int, criteria: dict[str, Any]) -> list[Trade]:
    query = select(Trade).where(Trade.account_id == account_id)
    for name, value in criteria.items():
        query = query.where(getattr(Trade, name) == _coerce(name, value))
    return list(session.scalars(query.order_by(Trade.id)))


def recompute_fill_fields(trade: Trade) -> None:
    """Rederive the fill-dependent columns the spreadsheet computes."""
    commission = trade.commission_per_share or Decimal("0")
    net_credit = trade.credit_debit - commission
    strike = trade.strike_price or Decimal("0")
    if trade.long_strike is not None:  # spread: at risk is the width, not the strike
        strike -= trade.long_strike
    # Rounded to the column scales so a re-run compares equal to what was stored.
    net_credit = net_credit.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    risk_capital = (strike - net_credit).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    margin_percent = trade.margin_percent if trade.margin_percent is not None else Decimal("1")
    contracts = Decimal(trade.num_of_contracts or 0)
    dte = (trade.expiration_date - trade.date_trade_open).days if trade.expiration_date else 0

    trade.net_credit_per_share = net_credit
    trade.risk_capital_per_share = risk_capital
    if trade.long_strike is not None or trade.margin_percent is not None or trade.margin_capital is not None:
        # OKW leaves both blank on covered calls (the shares are the collateral).
        trade.margin_capital = (
            risk_capital * CONTRACT_MULTIPLIER * contracts * margin_percent
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    trade.days_to_expiration = effective_days_to_expiration(dte)
    trade.arorc = calculate_arorc(
        calculate_rorc(net_credit, risk_capital, margin_percent * HUNDRED), dte
    ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _closing_outcome(session: Session, trade: Trade) -> str | None:
    return session.scalar(
        select(TradeEvent.event_type)
        .where(TradeEvent.trade_id == trade.id, TradeEvent.event_type != "OPEN")
        .order_by(TradeEvent.event_date.desc(), TradeEvent.id.desc())
        .limit(1)
    )


def _set_event(session: Session, trade: Trade, event_type: str, on: date) -> str | None:
    existing = session.scalar(
        select(TradeEvent).where(
            TradeEvent.trade_id == trade.id, TradeEvent.event_type == event_type
        )
    )
    if existing is None:
        session.add(
            TradeEvent(
                trade_id=trade.id, event_type=event_type, event_date=on, import_batch=EVENT_BATCH
            )
        )
        return f"added {event_type} {on}"
    if existing.event_date != on:
        old = existing.event_date
        existing.event_date = on
        return f"{event_type} {old} -> {on}"
    return None


def _add_trade(
    session: Session, account_id: int, correction: dict[str, Any], *, today: date
) -> tuple[Trade | None, str]:
    """Create a trade OKW never recorded, from the entry's key plus `add` fields."""
    trade_type = get_trade_type(session, correction["trade_type"])
    if trade_type is None:
        return None, f"error: unknown trade type {correction['trade_type']!r}"
    ticker = get_or_create_ticker(session, correction["ticker"])
    fields = {
        name: _coerce(name, correction[name]) for name in KEY_FIELDS if name in correction
    }
    fields.update({name: _coerce(name, value) for name, value in correction["add"].items()})
    fields.setdefault("num_of_shares", int(fields["num_of_contracts"]) * 100)
    fields.setdefault("commission_per_share", Decimal("0"))
    if "PUT" in correction["trade_type"] and fields.get("long_strike") is None:
        fields.setdefault("margin_percent", Decimal("1"))
    trade = Trade(
        account_id=account_id,
        ticker_id=ticker.id,
        trade_type_id=trade_type.id,
        import_batch=EVENT_BATCH,
        **fields,
    )
    recompute_fill_fields(trade)
    session.add(trade)
    session.flush()
    session.add(
        TradeEvent(
            trade_id=trade.id,
            event_type="OPEN",
            event_date=trade.date_trade_open,
            import_batch=EVENT_BATCH,
        )
    )
    return trade, (
        f"added {trade.num_of_contracts} ct @ {trade.credit_debit} (missing from OKW)"
    )


def apply_correction(
    session: Session, correction: dict[str, Any], *, today: date
) -> tuple[str, list[str], list[dict[str, Any]]]:
    """Returns (status, changes, removed_backups); status is
    applied / already_applied / error:<why>."""
    account_id = session.scalar(
        select(Account.id).where(Account.account_name == correction["account"])
    )
    if account_id is None:
        return f"error: unknown account {correction['account']!r}", [], []
    key = {name: correction[name] for name in KEY_FIELDS if name in correction}
    before = {**key, **correction.get("match", {})}
    fixes = dict(correction.get("set", {}))
    new_account = fixes.pop("account", None)
    after = {**key, **fixes}
    after_account_id = account_id
    if new_account is not None:
        after_account_id = session.scalar(select(Account.id).where(Account.account_name == new_account))
        if after_account_id is None:
            return f"error: unknown account {new_account!r}", [], []
    if "split" in correction:
        after["num_of_contracts"] = correction["split"]["assigned_contracts"]
    expected = int(correction.get("expect", 1))

    trades = _find(session, account_id, before)
    added_note = None
    if not trades and "add" in correction:
        added, added_note = _add_trade(session, account_id, correction, today=today)
        if added is None:
            return added_note, [], []
        trades = [added]
    if not trades:
        if correction.get("remove") or _find(session, after_account_id, after):
            return "already_applied", [], []
        return "error: no trade matches", [], []
    if len(trades) != expected:
        return (
            f"error: expected {expected} trade(s), matched {len(trades)} "
            f"({[t.id for t in trades]})",
            [],
            [],
        )

    if correction.get("remove"):
        removed = [_remove_trade(session, trade) for trade in trades]
        return (
            "applied",
            [f"#{b['trade']['id']}: removed ({correction['reason']})" for b in removed],
            removed,
        )

    changes: list[str] = []
    for trade in trades:
        trade_changes = [added_note] if added_note else []
        touched = [trade]
        for name, raw in fixes.items():
            value = _coerce(name, raw)
            if getattr(trade, name) != value:
                trade_changes.append(f"{name} {getattr(trade, name)} -> {value}")
                setattr(trade, name, value)
                if name == "ticker":
                    trade.ticker_id = get_or_create_ticker(session, value).id
                elif name == "trade_type":
                    trade_type = get_trade_type(session, value)
                    if trade_type is None:
                        return f"error: unknown trade type {value!r}", [], []
                    trade.trade_type_id = trade_type.id
        if new_account is not None and trade.account_id != after_account_id:
            trade_changes.append(f"account -> {new_account}")
            trade.account_id = after_account_id
        if correction.get("recompute"):
            snapshot = (
                trade.net_credit_per_share,
                trade.risk_capital_per_share,
                trade.margin_capital,
                trade.days_to_expiration,
                trade.arorc,
            )
            recompute_fill_fields(trade)
            if (
                trade.net_credit_per_share,
                trade.risk_capital_per_share,
                trade.margin_capital,
                trade.days_to_expiration,
                trade.arorc,
            ) != snapshot:
                trade_changes.append(f"recomputed arorc -> {trade.arorc}")
        for event_type, on in correction.get("events", {}).items():
            if note := _set_event(session, trade, event_type, date.fromisoformat(on)):
                trade_changes.append(note)
        for event_type, values in correction.get("event_fields", {}).items():
            event = session.scalar(
                select(TradeEvent).where(
                    TradeEvent.trade_id == trade.id, TradeEvent.event_type == event_type
                )
            )
            if event is None:
                return f"error: #{trade.id} has no {event_type} event", [], []
            values = dict(values)
            if "closing_debit" in values:
                # TOTAL DEBIT always follows the per-share closing debit x shares.
                closing = None if values["closing_debit"] is None else Decimal(str(values["closing_debit"]))
                derived = (
                    total_debit_for(closing, trade_shares(trade.num_of_shares, trade.num_of_contracts))
                    if closing
                    else Decimal("0")
                )
                given = values.get("total_debit")
                if given is not None and Decimal(str(given)) != derived:
                    return (
                        f"error: #{trade.id} total_debit {given} != closing_debit x shares ({derived})",
                        [],
                        [],
                    )
                values["total_debit"] = derived
            for name, raw in values.items():
                value = None if raw is None else Decimal(str(raw))
                if getattr(event, name) != value:
                    trade_changes.append(f"{event_type} {name} {getattr(event, name)} -> {value}")
                    setattr(event, name, value)
        if "outcome" in correction:
            if note := _change_outcome(session, trade, correction["outcome"]):
                trade_changes.append(note)
            elif not any(
                e.event_type == correction["outcome"]["to"]
                for e in session.scalars(select(TradeEvent).where(TradeEvent.trade_id == trade.id))
            ):
                # Neither the outcome to replace nor the corrected one exists:
                # the entry names the wrong current outcome. Fail loudly.
                return (
                    f"error: #{trade.id} has no {correction['outcome']['from']} "
                    f"(or {correction['outcome']['to']}) event to correct",
                    [],
                    [],
                )
        if "parent" in correction:
            parents = _find(session, account_id, correction["parent"])
            if len(parents) != 1:
                return (
                    f"error: parent matched {len(parents)} trade(s) ({[p.id for p in parents]})",
                    [],
                    [],
                )
            if trade.trade_parent_id != parents[0].id:
                trade.trade_parent_id = parents[0].id
                trade_changes.append(f"rolled from #{parents[0].id}")
        if "split" in correction:
            sibling, note = _split_off_expired(
                session, trade, correction["split"], today=today, reason=correction["reason"]
            )
            touched.append(sibling)
            trade_changes.append(note)
        session.flush()
        for row in touched:
            if note := _rebuild_lot(session, row):
                trade_changes.append(note if row is trade else f"#{row.id} {note}")
        if (correction.get("recompute") or added_note) and _closing_outcome(session, trade) == "EXPIRE":
            # Expired worthless: the realized result is the whole net credit.
            trade.result_net_credit = (
                trade.net_credit_per_share
                * CONTRACT_MULTIPLIER
                * Decimal(trade.num_of_contracts or 0)
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if trade.final_arorc is not None:
                trade.final_arorc = trade.arorc
        if trade_changes:
            audit = f"[statement truth {today}] {correction['reason']} ({'; '.join(trade_changes)})"
            trade.notes = f"{trade.notes}\n{audit}" if trade.notes else audit
            changes.append(f"#{trade.id}: " + "; ".join(trade_changes))
    return "applied", changes, []


def apply_corrections(
    session: Session, corrections: list[dict[str, Any]], *, today: date
) -> CorrectionResult:
    result = CorrectionResult()
    for correction in corrections:
        label = correction.get("id") or f"{correction['account']} {correction['ticker']}"
        status, changes, removed = apply_correction(session, correction, today=today)
        result.removed += removed
        if status == "applied" and not changes:
            status = "already_applied"  # matched, but everything was already correct
        if status == "applied":
            result.applied.append(f"{label}: " + (" | ".join(changes) or "no field changes"))
        elif status == "already_applied":
            result.already_applied.append(label)
        else:
            result.errors.append(f"{label}: {status}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument(
        "--backup", type=Path, default=Path("removed_by_statement_corrections.json")
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    corrections = json.loads(args.file.read_text())["corrections"]
    engine = create_engine(args.database_url)
    with sessionmaker(bind=engine)() as session:
        result = apply_corrections(session, corrections, today=date.today())
        if args.dry_run or result.errors:
            session.rollback()
        else:
            if result.removed:
                args.backup.write_text(json.dumps(result.removed, default=str, indent=1))
                print(f"backed up {len(result.removed)} removed trade(s) -> {args.backup}")
            session.commit()

    verb = "would apply" if args.dry_run else "applied"
    print(
        f"{verb} {len(result.applied)}, already applied {len(result.already_applied)}, "
        f"errors {len(result.errors)}"
    )
    for line in result.applied:
        print(f"  {line}")
    for line in result.errors:
        print(f"  ERROR {line}")
    if result.errors and not args.dry_run:
        print("Errors found — rolled back, nothing written.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
