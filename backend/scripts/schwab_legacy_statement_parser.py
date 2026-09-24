"""Parser for legacy TD Ameritrade monthly brokerage-statement PDFs
(2021-04 through mid-2023, before the Schwab platform migration).

Table is "Account Activity": Trade Date, Settle Date, Acct Type,
Transaction/Cash Activity, Description, Symbol/CUSIP, Quantity, Price,
Amount, Balance. No options activity exists anywhere in this era for any
of the three accounts (verified against all 60 legacy files) — just plain
equity Buy/Sell, cash dividends, bank interest, internal cash-sweep
journals (ignorable), and "Received - Other" stock-split/receipt entries
(share-count-only, zero cash impact).

Like the current-format parser, this uses word x/y-position clustering
rather than `extract_text()` line order: a transaction's Quantity value
can render on a wrapped continuation line below its Price/Amount, even
though visually reading top-to-bottom-then-left-to-right looks "clean".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pdfplumber

from scripts.schwab_statement_parser import _cluster_lines, _money, _qty, _extract_meta, StatementMeta

DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{2}$")

HEADER_COLUMNS = [
    "trade_date",
    "settle_date",
    "acct_type",
    "activity",
    "description",
    "symbol",
    "quantity",
    "price",
    "amount",
    "balance",
]

IGNORABLE_ACTIVITY_PREFIXES = (
    "Journal - Other",  # internal cash-sweep to/from the IDA feature; nets to 0
)


@dataclass
class LegacyTxnRow:
    trade_date: date | None
    settle_date: date | None
    activity: str | None
    description: str
    symbol: str | None
    quantity: Decimal | None
    price: Decimal | None
    amount: Decimal | None
    balance: Decimal | None
    raw_lines: list[str] = field(default_factory=list)


@dataclass
class ParsedLegacyStatement:
    meta: StatementMeta
    transactions: list[LegacyTxnRow] = field(default_factory=list)


def _parse_mmddyy(text: str) -> date | None:
    m = DATE_RE.match(text)
    if not m:
        return None
    mm, dd, yy = text.split("/")
    year = 2000 + int(yy)
    try:
        return date(year, int(mm), int(dd))
    except ValueError:
        return None


def _bucket(words: list[dict[str, Any]], col: tuple[float, float]) -> str:
    return " ".join(w["text"] for w in words if col[0] <= w["x0"] < col[1])


def _derive_legacy_columns(header_words: list[dict[str, Any]]) -> dict[str, tuple[float, float]] | None:
    date_positions = sorted(w["x0"] for w in header_words if w["text"] == "Date")
    type_positions = [w["x0"] for w in header_words if w["text"] == "Type"]
    cash_positions = [w["x0"] for w in header_words if w["text"] == "Cash"]
    desc_positions = [w["x0"] for w in header_words if w["text"] == "Description"]
    cusip_positions = [w["x0"] for w in header_words if w["text"] == "CUSIP"]
    qty_positions = [w["x0"] for w in header_words if w["text"] == "Quantity"]
    price_positions = [w["x0"] for w in header_words if w["text"] == "Price"]
    amount_positions = [w["x0"] for w in header_words if w["text"] == "Amount"]
    balance_positions = [w["x0"] for w in header_words if w["text"] == "Balance"]

    if not (
        len(date_positions) == 2
        and type_positions
        and cash_positions
        and desc_positions
        and cusip_positions
        and qty_positions
        and price_positions
        and amount_positions
        and balance_positions
    ):
        return None

    ordered = [
        ("trade_date", date_positions[0]),
        ("settle_date", date_positions[1]),
        ("acct_type", type_positions[0]),
        ("activity", cash_positions[0]),
        ("description", desc_positions[0]),
        ("symbol", cusip_positions[0]),
        ("quantity", qty_positions[0]),
        ("price", price_positions[0]),
        ("amount", amount_positions[0]),
        ("balance", balance_positions[0]),
    ]
    bounds: dict[str, tuple[float, float]] = {}
    for i, (key, x0) in enumerate(ordered):
        start = 0.0 if i == 0 else (ordered[i - 1][1] + x0) / 2
        end = 10_000.0 if i == len(ordered) - 1 else (x0 + ordered[i + 1][1]) / 2
        bounds[key] = (start, end)
    return bounds


STOP_MARKERS = (
    "ClosingBalance",
    "ForCashActivitytotals",
    "InsuredDepositAccountInterestCredited",
)


def parse_legacy_statement(path: str | Path) -> ParsedLegacyStatement:
    path = Path(path)
    with pdfplumber.open(path) as pdf:
        meta = _extract_meta(pdf, source_file=path.name)

        all_rows: list[tuple[list[dict[str, Any]], dict[str, tuple[float, float]]]] = []
        for page in pdf.pages:
            text = page.extract_text() or ""
            if "Account Activity" not in text:
                continue
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            lines = _cluster_lines(words)

            header_top: float | None = None
            stop_top: float | None = None
            page_columns: dict[str, tuple[float, float]] | None = None
            for line in lines:
                row_text = " ".join(w["text"] for w in line)
                compact = row_text.replace(" ", "")
                if row_text.strip() == "Date Date Type Cash Activity* Description CUSIP Quantity Price Amount Balance":
                    header_top = line[0]["top"]
                    page_columns = _derive_legacy_columns(line)
                elif header_top is not None and any(m in compact for m in STOP_MARKERS):
                    stop_top = line[0]["top"]
                    break
            if header_top is None or page_columns is None:
                continue

            for line in lines:
                top = line[0]["top"]
                if top <= header_top:
                    continue
                if stop_top is not None and top >= stop_top:
                    continue
                row_text = " ".join(w["text"] for w in line).strip()
                if row_text in ("Opening Balance", "") or row_text.startswith("Opening Balance"):
                    continue
                if re.fullmatch(r"page\s*\d+\s*of\s*\d+", row_text, re.IGNORECASE):
                    continue
                all_rows.append((line, page_columns))

        transactions: list[LegacyTxnRow] = []
        current: LegacyTxnRow | None = None
        current_lines: list[str] = []

        def flush() -> None:
            nonlocal current, current_lines
            if current is not None:
                current.raw_lines = current_lines
                transactions.append(current)
            current = None
            current_lines = []

        for line, columns in all_rows:
            trade_date_str = _bucket(line, columns["trade_date"]).strip()
            settle_date_str = _bucket(line, columns["settle_date"]).strip()
            activity_str = _bucket(line, columns["activity"]).strip()
            desc_str = _bucket(line, columns["description"]).strip()
            symbol_str = _bucket(line, columns["symbol"]).strip()
            qty_str = _bucket(line, columns["quantity"]).strip()
            price_str = _bucket(line, columns["price"]).strip()
            amount_str = _bucket(line, columns["amount"]).strip()
            balance_str = _bucket(line, columns["balance"]).strip()

            trade_date = _parse_mmddyy(trade_date_str)
            settle_date = _parse_mmddyy(settle_date_str)
            is_new_txn = trade_date is not None

            if is_new_txn:
                flush()
                current = LegacyTxnRow(
                    trade_date=trade_date,
                    settle_date=settle_date,
                    activity=activity_str or None,
                    description=desc_str,
                    symbol=symbol_str or None,
                    quantity=_qty(qty_str) if qty_str else None,
                    price=_money(price_str) if price_str else None,
                    amount=_money(amount_str) if amount_str else None,
                    balance=_money(balance_str) if balance_str else None,
                )
                current_lines = [" ".join(w["text"] for w in line)]
            else:
                if current is None:
                    continue
                if symbol_str and not current.symbol:
                    current.symbol = symbol_str
                if desc_str:
                    current.description = (current.description + " " + desc_str).strip()
                if qty_str and current.quantity is None:
                    current.quantity = _qty(qty_str)
                if price_str and current.price is None:
                    current.price = _money(price_str)
                if amount_str and current.amount is None:
                    current.amount = _money(amount_str)
                if balance_str and current.balance is None:
                    current.balance = _money(balance_str)
                current_lines.append(" ".join(w["text"] for w in line))
        flush()

    return ParsedLegacyStatement(meta=meta, transactions=transactions)
