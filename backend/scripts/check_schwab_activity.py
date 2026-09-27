"""Read-only: compare recent Schwab Trader API activity with the DB and OKW.

For trades too recent to be on a PDF statement yet, the Schwab API is the
authoritative source. This fetches TRADE and RECEIVE_AND_DELIVER activity
(the latter carries option expirations and assignments) for an account and
date range, turns every option and equity leg into a fill, and reports:

- option fills with no DB trade (missing from OKW or not imported yet)
- DB option trades in the window with no Schwab fill
- contract-count and price differences
- expirations / assignments the DB doesn't record
- equity buys/sells with no DB row

It never writes to the database. Raw API responses are saved (``--save-raw``)
so findings can be re-checked without calling Schwab again. Legs it can't
classify are listed, never guessed.

    python -m scripts.schwab_auth            # sign in first (tokens expire ~weekly)
    python -m scripts.check_schwab_activity --database-url ... --account roth \\
        --start 2026-09-01 --end 2026-09-27 [--save-raw roth_sep.json] [--from-raw roth_sep.json]
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

OCC_RE = re.compile(r"^(?P<root>[A-Z.]+)\s+(?P<yymmdd>\d{6})(?P<pc>[CP])(?P<strike>\d{8})$")
ACTIVITY_TYPES = "TRADE,RECEIVE_AND_DELIVER"
ACCOUNTS = {"rule1": "Rule 1", "roth": "Roth"}
PRICE_TOLERANCE = Decimal("0.005")
OPEN_DATE_WINDOW = timedelta(days=3)

OptionKey = tuple[str, date, Decimal, str]  # ticker, expiration, strike, PUT|CALL


@dataclass(frozen=True)
class OptionFill:
    activity_id: str
    on: date
    key: OptionKey
    action: str  # SELL_TO_OPEN, BUY_TO_OPEN, BUY_TO_CLOSE, SELL_TO_CLOSE, EXPIRE, ASSIGN
    contracts: Decimal
    price: Decimal | None
    description: str


@dataclass(frozen=True)
class EquityFill:
    activity_id: str
    on: date
    ticker: str
    shares: Decimal  # + bought, - sold
    price: Decimal | None
    description: str
    order_id: str | None = None


def parse_occ_symbol(symbol: str) -> OptionKey | None:
    """"ONON  260918P00027500" -> ("ONON", 2026-09-18, 27.5, "PUT")."""
    match = OCC_RE.match((symbol or "").strip())
    if not match:
        return None
    expiration = datetime.strptime(match.group("yymmdd"), "%y%m%d").date()
    strike = Decimal(match.group("strike")) / 1000
    right = "PUT" if match.group("pc") == "P" else "CALL"
    return match.group("root"), expiration, _strike(strike), right


def _strike(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _activity_date(txn: dict[str, Any]) -> date | None:
    raw = txn.get("tradeDate") or txn.get("time") or txn.get("settlementDate")
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date() if raw else None


def classify_legs(
    transactions: list[dict[str, Any]],
) -> tuple[list[OptionFill], list[EquityFill], list[str]]:
    """Every option / equity leg as a fill; anything ambiguous goes to `unclassified`."""
    options: list[OptionFill] = []
    equities: list[EquityFill] = []
    unclassified: list[str] = []
    for txn in transactions:
        activity_id = str(txn.get("activityId") or "")
        on = _activity_date(txn)
        description = str(txn.get("description") or "")
        blob = f"{description} {txn.get('type', '')}".upper()
        items = txn.get("transferItems") or []
        option_items = [i for i in items if (i.get("instrument") or {}).get("assetType") == "OPTION"]
        equity_items = [
            i for i in items if (i.get("instrument") or {}).get("assetType") in ("EQUITY", "ETF", "COLLECTIVE_INVESTMENT")
        ]
        assignment = "ASSIGN" in blob or "EXERCISE" in blob or (option_items and equity_items)
        for item in option_items:
            key = parse_occ_symbol((item.get("instrument") or {}).get("symbol", ""))
            amount = _decimal(item.get("amount")) or Decimal(0)
            if key is None or on is None:
                unclassified.append(f"{activity_id} {on} option leg {item.get('instrument')} {description}")
                continue
            effect = str(item.get("positionEffect") or "").upper()
            if "EXPIR" in blob:
                action = "EXPIRE"
            elif assignment:
                action = "ASSIGN"
            elif amount < 0:
                action = "SELL_TO_CLOSE" if effect == "CLOSING" else "SELL_TO_OPEN"
            elif amount > 0:
                action = "BUY_TO_OPEN" if effect == "OPENING" else "BUY_TO_CLOSE"
            else:
                unclassified.append(f"{activity_id} {on} zero-quantity option leg {key} {description}")
                continue
            if txn.get("type") == "TRADE" and action == "BUY_TO_CLOSE" and not effect:
                # Without positionEffect a long-leg buy (spread) and a buy-to-close
                # look alike; flag rather than guess.
                action = "BUY (open or close?)"
            options.append(
                OptionFill(activity_id, on, key, action, abs(amount), _decimal(item.get("price")), description)
            )
        if assignment:
            continue  # the equity leg of an assignment is the delivery, not a trade
        for item in equity_items:
            amount = _decimal(item.get("amount")) or Decimal(0)
            symbol = (item.get("instrument") or {}).get("symbol")
            if on is None or not symbol or amount == 0:
                continue
            order_id = str(txn["orderId"]) if txn.get("orderId") else None
            equities.append(
                EquityFill(activity_id, on, symbol, amount, _decimal(item.get("price")), description, order_id)
            )
    return options, _merge_order_fills(equities), unclassified


def _merge_order_fills(fills: list[EquityFill]) -> list[EquityFill]:
    """Partial fills of one order are one trade (as import_schwab stores them)."""
    merged: dict[tuple, EquityFill] = {}
    for fill in fills:
        key = (fill.order_id, fill.ticker, fill.on) if fill.order_id else (fill.activity_id,)
        if key in merged:
            prev = merged[key]
            shares = prev.shares + fill.shares
            price = None
            if prev.price is not None and fill.price is not None:
                price = (prev.price * prev.shares + fill.price * fill.shares) / shares
            merged[key] = replace(prev, shares=shares, price=price)
        else:
            merged[key] = fill
    return list(merged.values())


def _db_option_trades(conn, account: str, start: date, end: date) -> list[dict[str, Any]]:
    return [
        dict(r._mapping)
        for r in conn.execute(
            text(
                """select t.id, t.ticker, t.trade_type, t.date_trade_open, t.expiration_date,
                          t.strike_price, t.long_strike, t.num_of_contracts, t.credit_debit, t.import_batch,
                          (select string_agg(e.event_type || ' ' || e.event_date, ', ' order by e.event_date)
                             from trade_events e where e.trade_id = t.id) as events
                   from trades t join accounts a on a.id = t.account_id
                   where a.account_name = :a and t.trade_type not in ('BTO', 'STC')
                     and t.date_trade_open <= :e and t.expiration_date >= :s"""
            ),
            {"a": account, "s": start - OPEN_DATE_WINDOW, "e": end},
        )
    ]


def _db_equity_trades(conn, account: str, start: date, end: date) -> list[dict[str, Any]]:
    return [
        dict(r._mapping)
        for r in conn.execute(
            text(
                """select t.id, t.ticker, t.trade_type, t.date_trade_open, t.num_of_shares, t.price_per_share
                   from trades t join accounts a on a.id = t.account_id
                   where a.account_name = :a and t.trade_type in ('BTO', 'STC')
                     and t.date_trade_open between :s and :e"""
            ),
            {"a": account, "s": start - OPEN_DATE_WINDOW, "e": end + OPEN_DATE_WINDOW},
        )
    ]


def _db_key(trade: dict[str, Any], strike: Decimal) -> OptionKey:
    right = "PUT" if "PUT" in trade["trade_type"] else "CALL"
    return trade["ticker"], trade["expiration_date"], _strike(strike), right


def _assignment_source(fill: EquityFill, db_options, options: list[OptionFill]) -> str | None:
    """None if `fill` is not an assignment delivery; "" if the DB records it; else a finding.

    Schwab books assignment deliveries as ordinary equity TRADEs: whole
    100-share lots at exactly an option's strike, shortly after its expiration.
    """
    if fill.shares % 100 != 0 or fill.price is None:
        return None
    right = "PUT" if fill.shares > 0 else "CALL"
    near = lambda exp: exp is not None and timedelta(0) <= fill.on - exp <= OPEN_DATE_WINDOW + timedelta(days=1)
    trades = [
        t for t in db_options
        if t["ticker"] == fill.ticker and right in t["trade_type"] and _strike(t["strike_price"]) == _strike(fill.price)
        and near(t["expiration_date"])
    ]
    schwab_assign = [
        f for f in options
        if f.action == "ASSIGN" and f.key[0] == fill.ticker and f.key[3] == right and f.key[2] == _strike(fill.price)
    ]
    if not trades and not schwab_assign:
        return None
    label = f"{fill.on} {abs(fill.shares)} {fill.ticker} @ {fill.price} (#{fill.activity_id})"
    if not trades:
        return f"[assignment_missing_in_db] {label}: delivery from a Schwab assignment with no DB trade"
    if not any("ASSIGN" in (t["events"] or "") for t in trades):
        events = " | ".join(f"#{t['id']} exp {t['expiration_date']}: {t['events']}" for t in trades)
        return f"[assignment_not_recorded] {label}: DB {events}"
    return ""


def compare(
    options, equities, db_options, db_equities, start: date, okw_keys: set[OptionKey] | None = None
) -> list[str]:
    report: list[str] = []
    by_key: dict[OptionKey, list[dict[str, Any]]] = defaultdict(list)
    for trade in db_options:
        by_key[_db_key(trade, trade["strike_price"])].append(trade)
        if trade["long_strike"] is not None:  # spread long leg
            by_key[_db_key(trade, trade["long_strike"])].append(trade)

    opened: dict[OptionKey, list[OptionFill]] = defaultdict(list)
    for fill in options:
        if fill.action in ("SELL_TO_OPEN", "BUY_TO_OPEN", "BUY (open or close?)"):
            opened[fill.key].append(fill)
    for key, fills in sorted(opened.items(), key=lambda kv: min(f.on for f in kv[1])):
        trades = [
            t for t in by_key.get(key, [])
            if any(abs(t["date_trade_open"] - f.on) <= OPEN_DATE_WINDOW for f in fills)
        ]
        schwab_ct = sum(f.contracts for f in fills if f.action != "BUY (open or close?)")
        label = f"{key[0]} ${key[2]} {key[3]} exp {key[1]}"
        fills_txt = "; ".join(f"{f.on} {f.action} {f.contracts}@{f.price} (#{f.activity_id})" for f in fills)
        if okw_keys is not None and key not in okw_keys:
            report.append(f"[missing_in_okw] {label}: Schwab {fills_txt}")
        if not trades:
            report.append(f"[missing_in_db] {label}: Schwab {fills_txt}")
            continue
        if all(_strike(t["strike_price"]) != key[2] for t in trades):
            continue  # spread long leg: checked through its short leg's net credit
        db_ct = sum(Decimal(t["num_of_contracts"] or 0) for t in trades if _strike(t["strike_price"]) == key[2])
        if schwab_ct and db_ct != schwab_ct:
            report.append(f"[contract_count] {label}: Schwab {schwab_ct} vs DB {db_ct} ({[t['id'] for t in trades]}) | {fills_txt}")
        for t in trades:
            if _strike(t["strike_price"]) != key[2]:
                continue
            prices = {f.price for f in fills if f.price is not None}
            if t["long_strike"] is not None:  # spread: DB stores the net credit
                long_fills = opened.get((key[0], key[1], _strike(t["long_strike"]), key[3]), [])
                prices = {
                    s.price - l.price
                    for s in fills for l in long_fills
                    if s.on == l.on and s.price is not None and l.price is not None
                }
            if prices and not any(abs(Decimal(t["credit_debit"]) - p) <= PRICE_TOLERANCE for p in prices):
                report.append(f"[price] {label}: DB #{t['id']} credit {t['credit_debit']} vs Schwab {sorted(prices)}")

    for fill in options:
        if fill.action not in ("EXPIRE", "ASSIGN", "BUY_TO_CLOSE", "SELL_TO_CLOSE"):
            continue
        trades = by_key.get(fill.key, [])
        want = {"EXPIRE": "EXPIRE", "ASSIGN": "ASSIGN"}.get(fill.action, "CLOSE/ROLL")
        label = f"{fill.key[0]} ${fill.key[2]} {fill.key[3]} exp {fill.key[1]}"
        if not trades:
            report.append(f"[missing_in_db] {label}: Schwab {fill.on} {fill.action} {fill.contracts} (#{fill.activity_id}), no DB trade")
            continue
        events = " | ".join(f"#{t['id']}: {t['events']}" for t in trades)
        if not any(w in (t["events"] or "") for t in trades for w in want.split("/")):
            report.append(f"[outcome] {label}: Schwab {fill.on} {fill.action} {fill.contracts}; DB {events}")

    schwab_keys = {f.key for f in options}
    for t in db_options:
        key = _db_key(t, t["strike_price"])
        if t["date_trade_open"] >= start and key not in schwab_keys:
            report.append(
                f"[no_schwab_fill] DB #{t['id']} {t['trade_type']} {key[0]} ${key[2]} exp {key[1]} "
                f"opened {t['date_trade_open']} {t['num_of_contracts']}ct @ {t['credit_debit']} [{t['import_batch']}]"
            )

    for fill in equities:
        kind = "BTO" if fill.shares > 0 else "STC"
        delivered = _assignment_source(fill, db_options, options)
        if delivered is not None:
            if delivered:
                report.append(delivered)
            continue
        match = [
            t for t in db_equities
            if t["ticker"] == fill.ticker and t["trade_type"] == kind
            and abs(t["date_trade_open"] - fill.on) <= OPEN_DATE_WINDOW
            and Decimal(t["num_of_shares"] or 0) == abs(fill.shares)
        ]
        if not match:
            report.append(f"[equity_missing_in_db] {fill.on} {kind} {fill.ticker} {abs(fill.shares)} @ {fill.price} (#{fill.activity_id}) {fill.description}")
    return report


def okw_option_keys(path: Path, sheet: str) -> set[OptionKey]:
    """Every (ticker, exp, strike, right) in an OKW sheet, spread long legs included."""
    from scripts.okw_parser import parse_trade_sheet
    from scripts.xlsx_compat import load_workbook_safely

    keys: set[OptionKey] = set()
    for parsed in parse_trade_sheet(load_workbook_safely(path, data_only=True)[sheet]):
        if parsed.expiration_date is None or parsed.short_strike is None:
            continue
        right = "PUT" if "PUT" in parsed.trade_type_name.upper() else "CALL"
        for strike in (parsed.short_strike, parsed.long_strike):
            if strike is not None:
                keys.add((parsed.ticker, parsed.expiration_date, _strike(strike), right))
    return keys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--account", required=True, choices=sorted(ACCOUNTS))
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--schwab-account-hash", default=None)
    parser.add_argument("--okw-file", type=Path, default=None, help="Also flag Schwab opens missing from this OKW workbook.")
    parser.add_argument("--okw-sheet", default=None, help='e.g. "TRADES 2026" or "ROTH TRADES 2026".')
    parser.add_argument("--save-raw", type=Path, default=None, help="Write the raw API response here.")
    parser.add_argument("--from-raw", type=Path, default=None, help="Use a saved response instead of calling Schwab.")
    args = parser.parse_args(argv)

    if args.from_raw:
        transactions = json.loads(args.from_raw.read_text())
    else:
        from scripts.import_schwab import _account_hash
        from scripts.schwab_client import SchwabClient
        from scripts.schwab_tokens import DEFAULT_TOKEN_PATH

        with SchwabClient(tokens_path=DEFAULT_TOKEN_PATH) as client:
            transactions = client.fetch_transactions(
                _account_hash(args.account, args.schwab_account_hash),
                start=args.start, end=args.end, types=ACTIVITY_TYPES,
            )
        if args.save_raw:
            args.save_raw.write_text(json.dumps(transactions, indent=1, default=str))
            print(f"saved {len(transactions)} raw transactions -> {args.save_raw}")

    okw_keys = okw_option_keys(args.okw_file, args.okw_sheet) if args.okw_file else None
    options, equities, unclassified = classify_legs(transactions)
    account = ACCOUNTS[args.account]
    with create_engine(args.database_url).connect() as conn:
        report = compare(
            options, equities,
            _db_option_trades(conn, account, args.start, args.end),
            _db_equity_trades(conn, account, args.start, args.end),
            args.start,
            okw_keys,
        )
    print(f"{account} {args.start}..{args.end}: {len(transactions)} Schwab transactions, "
          f"{len(options)} option legs, {len(equities)} equity legs")
    for line in report:
        print(line)
    for line in unclassified:
        print(f"[unclassified] {line}")
    if not report and not unclassified:
        print("everything matches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
