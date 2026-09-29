"""Read-only: find every option roll in Schwab order history and check the DB.

A roll is one Schwab order that buys to close an option and sells to open
another on the same underlying (e.g. order 1002557821629 on 2025-01-07: bought
back 4 TSLA 01/17/25 360C @ ~38.27, sold 4 TSLA 01/24/25 450C @ ~4.77). The
statement PDFs show both fills but not that they were one order, so rolls
are confirmed here from the Trader API's `orderId`.

For each roll it checks the DB:

- `missing_leg`: no DB trade for the closed leg or the opened leg
- `roll_date`: the closed leg's outcome isn't ROLL on the order date
- `open_date`: the opened leg's `date_trade_open` isn't the order date
- `unlinked`: the opened leg's `trade_parent_id` doesn't point at the closed leg
- `net_credit`: the opened leg's credit isn't the order's net per contract
  (sale - buyback). A roll leg stores the order's NET (user rule, 2026-09-28:
  OKW sometimes carries an earlier leg's credit forward or records the raw
  fill), so the net is proposed — unless OKW booked the roll's cost as a
  closing debit on the rolled leg, which the user's accounting keeps.

The fixable kinds are written as proposed `statement_corrections.json`
entries (natural keys) for review. The API reaches back to about late 2024;
older rolls need the statements.

    python -m scripts.check_rolls --database-url ... --account rule1 \\
        --start 2024-11-01 --end 2026-09-27 [--out proposed_rolls.json] [--from-raw raw.json]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from scripts.check_schwab_activity import OptionKey, parse_occ_symbol

ACCOUNTS = {"rule1": "Rule 1", "roth": "Roth"}
CLOSING_EVENTS = ("ROLL", "EXPIRE", "CLOSE", "ASSIGN")
CENT = Decimal("0.01")


@dataclass
class Roll:
    order_id: str
    on: date
    closed: dict[OptionKey, list[tuple[Decimal, Decimal]]] = field(default_factory=dict)  # key -> [(qty, price)]
    opened: dict[OptionKey, list[tuple[Decimal, Decimal]]] = field(default_factory=dict)
    other: bool = False  # the order also had legs that are neither (spread open/close)

    @staticmethod
    def _avg(fills: list[tuple[Decimal, Decimal]]) -> Decimal:
        qty = sum(q for q, _ in fills)
        return sum(q * p for q, p in fills) / qty

    def net(self, opened_key: OptionKey) -> Decimal | None:
        """Per-contract net credit of the opened leg: its sale minus the buyback."""
        if len(self.closed) != 1:
            return None
        [closed] = self.closed.values()
        return (self._avg(self.opened[opened_key]) - self._avg(closed)).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )


def find_rolls(transactions: list[dict[str, Any]]) -> list[Roll]:
    """Orders with both a closing buy and an opening sell of options on one underlying."""
    by_order: dict[str, Roll] = {}
    for txn in transactions:
        order_id = txn.get("orderId")
        if not order_id or txn.get("type") != "TRADE":
            continue
        on = datetime.fromisoformat(str(txn["tradeDate"]).replace("Z", "+00:00")).date()
        for item in txn.get("transferItems") or []:
            instrument = item.get("instrument") or {}
            if instrument.get("assetType") != "OPTION":
                continue
            key = parse_occ_symbol(instrument.get("symbol", ""))
            amount = Decimal(str(item.get("amount") or 0))
            if key is None or amount == 0 or item.get("price") is None:
                continue
            effect = str(item.get("positionEffect") or "").upper()
            roll = by_order.setdefault(str(order_id), Roll(str(order_id), on))
            if amount > 0 and effect == "CLOSING":
                side = roll.closed
            elif amount < 0 and effect == "OPENING":
                side = roll.opened
            else:  # a spread's long leg, or a closing sale: not part of a roll
                roll.other = True
                continue
            side.setdefault(key, []).append((abs(amount), Decimal(str(item["price"]))))
    return [
        roll
        for roll in by_order.values()
        if roll.closed
        and roll.opened
        and not roll.other
        and len({k[0] for k in roll.closed} | {k[0] for k in roll.opened}) == 1
    ]


@dataclass
class Finding:
    kind: str
    roll: Roll
    detail: str
    proposal: dict[str, Any] | None = None


def _key_fields(trade: dict[str, Any], account_name: str) -> dict[str, Any]:
    return {
        "account": account_name,
        "ticker": trade["ticker"],
        "trade_type": trade["trade_type"],
        "date_trade_open": str(trade["date_trade_open"]),
        "expiration_date": str(trade["expiration_date"]),
        "strike_price": f"{Decimal(trade['strike_price']):.2f}",
    }


def _match(
    trades: list[dict[str, Any]], key: OptionKey, *, near: date, opened: bool, net: Decimal | None = None
) -> dict | None:
    ticker, expiration, strike, right = key
    candidates = [
        t
        for t in trades
        if t["ticker"] == ticker
        and t["expiration_date"] == expiration
        and right in t["trade_type"]
        and Decimal(t["strike_price"]).quantize(CENT) == strike
        and (
            abs((t["date_trade_open"] - near).days) <= 3
            if opened
            else t["date_trade_open"] <= near
        )
    ]
    if opened:
        # Two orders can roll into two same-key legs (TSLA 435C x2 on 2026-05-29):
        # the one whose credit is this order's net is this order's leg.
        candidates.sort(
            key=lambda t: (
                abs((t["date_trade_open"] - near).days),
                abs(Decimal(t["credit_debit"]) - net) if net is not None else 0,
            )
        )
    else:
        # Several same-key trades (e.g. 2 CELH puts assigned, 1 rolled): the
        # leg that was rolled is the one whose outcome is ROLL.
        candidates.sort(
            key=lambda t: (any(e[0] == "ROLL" for e in t["events"]), t["date_trade_open"]),
            reverse=True,
        )
    return candidates[0] if candidates else None


def check(rolls: list[Roll], trades: list[dict[str, Any]], account_name: str) -> list[Finding]:
    findings: list[Finding] = []
    for roll in sorted(rolls, key=lambda r: r.on):
        label = lambda k: f"{k[0]} {k[3][0]} ${k[2]:g} exp {k[1]:%m/%d/%y}"  # noqa: E731
        evidence = (
            f"Schwab order {roll.order_id} on {roll.on}: bought back "
            + ", ".join(f"{label(k)} {sum(q for q, _ in f):g} @ {Roll._avg(f):.4f}" for k, f in roll.closed.items())
            + "; sold "
            + ", ".join(f"{label(k)} {sum(q for q, _ in f):g} @ {Roll._avg(f):.4f}" for k, f in roll.opened.items())
        )
        parents = {k: _match(trades, k, near=roll.on, opened=False) for k in roll.closed}
        children = {k: _match(trades, k, near=roll.on, opened=True, net=roll.net(k)) for k in roll.opened}
        for k, t in {**parents, **children}.items():
            if t is None:
                findings.append(Finding("missing_leg", roll, f"no DB trade for {label(k)} — {evidence}"))
        parent = next(iter(parents.values())) if len(parents) == 1 else None
        if parent is not None:
            outcomes = [e for e in parent["events"] if e[0] in CLOSING_EVENTS]
            if not any(tuple(e[:2]) == ("ROLL", roll.on) for e in outcomes):
                current = tuple(outcomes[-1][:2]) if outcomes else None
                proposal: dict[str, Any] = {**_key_fields(parent, account_name)}
                if current is None or current[0] == "ROLL":
                    proposal["events"] = {"ROLL": str(roll.on)}
                else:
                    proposal["outcome"] = {"from": current[0], "to": "ROLL", "date": str(roll.on)}
                findings.append(
                    Finding(
                        "roll_date",
                        roll,
                        f"#{parent['id']} outcome {current} vs ROLL {roll.on} — {evidence}",
                        {**proposal, "statement": evidence, "reason": "ROLL dated on the roll order"},
                    )
                )
        for k, child in children.items():
            if child is None:
                continue
            fixes: dict[str, Any] = {}
            notes = []
            if child["date_trade_open"] != roll.on:
                fixes["set"] = {"date_trade_open": str(roll.on)}
                fixes["recompute"] = True
                notes.append(f"opened {child['date_trade_open']} vs order {roll.on}")
            if parent is not None and child["trade_parent_id"] != parent["id"]:
                fixes["parent"] = {
                    k2: v for k2, v in _key_fields(parent, account_name).items() if k2 != "account"
                }
                notes.append(f"parent {child['trade_parent_id']} vs #{parent['id']}")
            if fixes:
                findings.append(
                    Finding(
                        "open_date" if "set" in fixes else "unlinked",
                        roll,
                        f"#{child['id']} " + "; ".join(notes) + f" — {evidence}",
                        {
                            **_key_fields(child, account_name),
                            # credit disambiguates same-key legs (two rolls into one strike)
                            "match": {"credit_debit": f"{Decimal(child['credit_debit']):.4f}"},
                            **fixes,
                            "statement": evidence,
                            "reason": "Roll continuation per the Schwab order: " + "; ".join(notes),
                        },
                    )
                )
            net = roll.net(k)
            # When OKW books the roll's cost as a closing debit on the rolled
            # leg (user's accounting, GOOG WZ/XA: credit 1.78, debit 0.35), each
            # leg keeps its own OKW credit and the debit is subtracted from that
            # leg's net — don't propose the order net onto the new leg.
            parent_debit = parent is not None and any(
                e[0] == "ROLL" and len(e) > 2 and e[2] for e in parent["events"]
            )
            if (
                net is not None
                and not parent_debit
                and Decimal(child["credit_debit"]).quantize(CENT) != net.quantize(CENT)
            ):
                findings.append(
                    Finding(
                        "net_credit",
                        roll,
                        f"#{child['id']} credit {child['credit_debit']} vs order net {net} — {evidence}",
                        {
                            **_key_fields(child, account_name),
                            "match": {"credit_debit": f"{Decimal(child['credit_debit']):.4f}"},
                            "set": {"credit_debit": f"{net.quantize(CENT)}"},
                            "recompute": True,
                            "statement": evidence,
                            "reason": f"Roll leg stores the order's net {net.quantize(CENT)} "
                            f"(OKW {Decimal(child['credit_debit']):.2f})",
                        },
                    )
                )
    return findings


def _db_trades(database_url: str, account_name: str) -> list[dict[str, Any]]:
    with create_engine(database_url).connect() as conn:
        rows = [
            dict(r._mapping)
            for r in conn.execute(
                text(
                    """select t.id, t.ticker, t.trade_type, t.date_trade_open, t.expiration_date,
                              t.strike_price, t.credit_debit, t.trade_parent_id
                       from trades t join accounts a on a.id = t.account_id
                       where a.account_name = :a and t.trade_type not in ('BTO', 'STC')
                         and t.long_strike is null"""
                ),
                {"a": account_name},
            )
        ]
        events: dict[int, list[tuple]] = defaultdict(list)
        for trade_id, event_type, event_date, closing_debit in conn.execute(
            text(
                """select e.trade_id, e.event_type, e.event_date, e.closing_debit from trade_events e
                   join trades t on t.id = e.trade_id join accounts a on a.id = t.account_id
                   where a.account_name = :a order by e.event_date, e.id"""
            ),
            {"a": account_name},
        ):
            events[trade_id].append((event_type, event_date, closing_debit))
    for row in rows:
        row["events"] = events[row["id"]]
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--account", required=True, choices=sorted(ACCOUNTS))
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--schwab-account-hash", default=None)
    parser.add_argument("--from-raw", type=Path, default=None)
    parser.add_argument("--save-raw", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=Path("proposed_rolls.json"))
    args = parser.parse_args(argv)

    if args.from_raw:
        transactions = json.loads(args.from_raw.read_text())
    else:
        from scripts.import_schwab import _account_hash
        from scripts.schwab_client import SchwabClient

        transactions = []
        with SchwabClient() as client:
            month = args.start
            while month <= args.end:  # the API is queried a month at a time
                upto = min(args.end, (month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1))
                transactions += client.fetch_transactions(
                    _account_hash(args.account, args.schwab_account_hash), start=month, end=upto, types="TRADE"
                )
                month = upto + timedelta(days=1)
        if args.save_raw:
            args.save_raw.write_text(json.dumps(transactions, default=str))

    rolls = find_rolls(transactions)
    account_name = ACCOUNTS[args.account]
    findings = check(rolls, _db_trades(args.database_url, account_name), account_name)
    counts: dict[str, int] = defaultdict(int)
    for f in findings:
        counts[f.kind] += 1
        print(f"[{f.kind}] {f.detail}")
    proposals = [f.proposal for f in findings if f.proposal is not None]
    args.out.write_text(json.dumps({"proposed": proposals}, indent=2, default=str))
    print(f"{account_name}: {len(transactions)} transactions, {len(rolls)} rolls; findings {dict(counts)}")
    print(f"wrote {len(proposals)} proposed correction(s) for review -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
