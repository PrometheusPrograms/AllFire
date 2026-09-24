"""Parser for Schwab monthly brokerage-statement PDFs (current format, 2023+).

Extracts the "Transaction Details" table (trades, dividends, interest,
expirations, assignments, transfers) plus statement metadata (account
nickname, account number, statement period). Pure parsing — no DB access.

Row layout is column-position based (pdfplumber word x0/top), not
whitespace-based, because Schwab's PDF text extraction collapses spaces
between adjacent columns (e.g. "SLV" + "06/13/2025" -> "SLV06/13/2025").

Multi-line transactions: Schwab wraps a single transaction's Category+Action
onto one line and Date+Symbol+Description+Quantity+Price+Amount onto the
next, then follows with 0+ continuation lines (strike/expiration detail,
commission/fee breakdown) with no Date. We stitch these into one logical
record per transaction using vertical proximity + "does this line start a
new transaction" heuristics (a new Date, or a new top-level Category word
while the previous transaction is already "closed" by having an Amount).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pdfplumber

CATEGORIES = {
    "Sale",
    "Purchase",
    "Dividend",
    "Interest",
    "Other",
    "Journal",
    "Transfer",
    "ForeignTax",
    "Fee",
    "Reinvest",
    "AdvisorFee",
    "MiscCashEntry",
    "Deposit",
    "Withdrawal",
    "Redemption",
}

# Header labels, in column order, used to derive per-statement column
# boundaries dynamically (the exact x-positions shift slightly between
# statement template versions, so we never hardcode them).
HEADER_LABELS = [
    ("date", "Date"),
    ("category", "Category"),
    ("action", "Action"),
    ("symbol", "CUSIP"),
    ("description", "Description"),
    ("quantity", "Quantity"),
    ("price", "perShare($)"),
    ("charges", "Interest($)"),
    ("amount", "Amount($)"),
]
# Retirement-account statements (Roth) omit the realized gain/loss column
# entirely since it's not tax-relevant there.
OPTIONAL_HEADER_LABELS = [("realized", "Gain/(Loss)($)")]

DATE_RE = re.compile(r"^\d{2}/\d{2}$")
MONEY_RE = re.compile(r"^\(?-?\$?[\d,]+\.\d{2}\)?,?(\(ST\)|\(LT\))?$")
QTY_RE = re.compile(r"^\(?-?[\d,]+\.\d{4}\)?$")


@dataclass
class StatementMeta:
    account_nickname: str | None
    account_number: str | None
    period_start: date | None
    period_end: date | None
    source_file: str


@dataclass
class TxnRow:
    """One row from the Transaction Details table, loosely parsed."""

    date: date | None
    category: str | None
    action: str | None
    symbol_cusip: str | None
    description: str
    quantity: Decimal | None
    price: Decimal | None
    charges: Decimal | None
    amount: Decimal | None
    realized_gain: Decimal | None
    raw_lines: list[str] = field(default_factory=list)


@dataclass
class TxnSummary:
    beginning_cash: Decimal | None
    deposits: Decimal | None
    withdrawals: Decimal | None
    purchases: Decimal | None
    sales: Decimal | None
    dividends_interest: Decimal | None
    expenses: Decimal | None
    ending_cash: Decimal | None


@dataclass
class ParsedStatement:
    meta: StatementMeta
    transactions: list[TxnRow] = field(default_factory=list)
    summary: TxnSummary | None = None


def _money(text: str) -> Decimal | None:
    # Legacy TDA statements render "$" as a separate word with a space before
    # the number (e.g. "$ (9.59)"), so strip whitespace/$ before checking for
    # a parens-as-negative wrapper.
    text = text.strip().rstrip(",").replace(" ", "").replace("$", "")
    if not text or text == "-":
        return None
    neg = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "")
    if "(ST)" in text or "(LT)" in text:
        text = text.replace("(ST)", "").replace("(LT)", "")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return -value if neg else value


def _qty(text: str) -> Decimal | None:
    text = text.strip()
    neg = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return -value if neg else value


MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11,
    "December": 12,
}


def _parse_period(text: str) -> tuple[date | None, date | None]:
    """Parse e.g. 'June1-30,2025' or 'May28-June24,2025' (rare cross-month)."""
    m = re.match(
        r"([A-Za-z]+)(\d{1,2})-(?:([A-Za-z]+))?(\d{1,2}),(\d{4})", text.strip()
    )
    if not m:
        return None, None
    mon1, day1, mon2, day2, year = m.groups()
    year_i = int(year)
    mon1_i = MONTHS.get(mon1)
    if mon1_i is None:
        return None, None
    mon2_i = MONTHS.get(mon2) if mon2 else mon1_i
    try:
        start = date(year_i, mon1_i, int(day1))
        end_year = year_i if mon2_i is not None and mon2_i >= mon1_i else year_i
        end = date(end_year, mon2_i, int(day2))
    except ValueError:
        return None, None
    return start, end


def _extract_meta(pdf: pdfplumber.PDF, source_file: str) -> StatementMeta:
    text = pdf.pages[0].extract_text() or ""
    nickname = None
    m = re.search(r"AccountNickname\s*\n\s*([A-Za-z0-9 ]+?)\s*\n", text)
    if m:
        nickname = m.group(1).strip()
    number = None
    m = re.search(r"(\d{4}-\d{4})", text)
    if m:
        number = m.group(1)
    idx = text.find("StatementPeriod")
    m = None
    if idx != -1:
        m = re.search(
            r"([A-Za-z]+\d{1,2}-[A-Za-z]*\d{1,2},\d{4})", text[idx : idx + 200]
        )
    start = end = None
    if m:
        start, end = _parse_period(m.group(1))
    return StatementMeta(
        account_nickname=nickname,
        account_number=number,
        period_start=start,
        period_end=end,
        source_file=source_file,
    )


def _cluster_lines(words: list[dict[str, Any]], tol: float = 2.5) -> list[list[dict[str, Any]]]:
    """Group words into visual lines, tolerant of sub-pixel baseline jitter."""
    words = sorted(words, key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict[str, Any]]] = []
    for w in words:
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    for line in lines:
        line.sort(key=lambda w: w["x0"])
    return lines


def _bucket(words: list[dict[str, Any]], col: tuple[float, float]) -> str:
    return " ".join(w["text"] for w in words if col[0] <= w["x0"] < col[1])


def _derive_columns(header_words: list[dict[str, Any]]) -> dict[str, tuple[float, float]] | None:
    """Compute column boundaries from the header row's own word positions,
    since exact x-coordinates shift a bit between statement template
    versions/pages. Boundaries are midpoints between adjacent header labels.
    """
    positions: dict[str, float] = {}
    for key, label in HEADER_LABELS + OPTIONAL_HEADER_LABELS:
        for w in header_words:
            if w["text"] == label:
                positions[key] = w["x0"]
                break
    if any(key not in positions for key, _ in HEADER_LABELS):
        return None
    present_labels = [key for key, _ in HEADER_LABELS + OPTIONAL_HEADER_LABELS if key in positions]
    ordered = [(key, positions[key]) for key in present_labels]
    bounds: dict[str, tuple[float, float]] = {}
    for i, (key, x0) in enumerate(ordered):
        start = 0.0 if i == 0 else (ordered[i - 1][1] + x0) / 2
        end = 10_000.0 if i == len(ordered) - 1 else (x0 + ordered[i + 1][1]) / 2
        bounds[key] = (start, end)
    for key, _ in OPTIONAL_HEADER_LABELS:
        bounds.setdefault(key, (10_000.0, 10_001.0))
    return bounds


STOP_MARKERS = (
    "TotalTransactions",
    "DatecolumnRepresents",
    "Pending/OpenActivity",
    "EndnotesForYourAccount",
    "TermsandConditions",
)


def _extract_txn_summary(pdf: pdfplumber.PDF) -> TxnSummary | None:
    for page in pdf.pages:
        text = page.extract_text() or ""
        compact = text.replace(" ", "")
        if "BeginningCash*asof" not in compact:
            continue
        idx = compact.index("BeginningCash*asof")
        tail = compact[idx:]
        nl = tail.find("\n")
        if nl == -1:
            continue
        rest = tail[nl + 1 :]
        nl2 = rest.find("\n")
        value_line = rest[:nl2] if nl2 != -1 else rest
        tokens = re.findall(r"\(?\$[\d,]+\.\d{2}\)?", value_line)
        if len(tokens) != 8:
            return None
        vals = [_money(t) for t in tokens]
        return TxnSummary(
            beginning_cash=vals[0],
            deposits=vals[1],
            withdrawals=vals[2],
            purchases=vals[3],
            sales=vals[4],
            dividends_interest=vals[5],
            expenses=vals[6],
            ending_cash=vals[7],
        )
    return None


def _find_table_pages(pdf: pdfplumber.PDF) -> list[int]:
    pages = []
    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        if "Transaction Details" in text or "TransactionDetails" in text.replace(" ", ""):
            pages.append(i)
    return pages


def parse_statement(path: str | Path) -> ParsedStatement:
    path = Path(path)
    with pdfplumber.open(path) as pdf:
        meta = _extract_meta(pdf, source_file=path.name)
        summary = _extract_txn_summary(pdf)
        table_pages = _find_table_pages(pdf)

        all_rows: list[tuple[list[dict[str, Any]], dict[str, tuple[float, float]]]] = []
        for page_idx in table_pages:
            page = pdf.pages[page_idx]
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            lines = _cluster_lines(words)

            header_top: float | None = None
            stop_top: float | None = None
            page_columns: dict[str, tuple[float, float]] | None = None
            for line in lines:
                row_text = " ".join(w["text"] for w in line)
                compact = row_text.replace(" ", "")
                if row_text.startswith("Date Category") or compact.startswith("DateCategory"):
                    header_top = line[0]["top"]
                    page_columns = _derive_columns(line)
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
                row_text = " ".join(w["text"] for w in line)
                if row_text.strip().startswith(("Symbol/", "Date Category")):
                    continue
                if row_text.strip() in ("(continued)",):
                    continue
                if re.fullmatch(r"\d+of\d+", row_text.strip().replace(" ", "")):
                    continue
                all_rows.append((line, page_columns))

        transactions: list[TxnRow] = []
        current: TxnRow | None = None
        current_lines: list[str] = []

        def flush() -> None:
            nonlocal current, current_lines
            if current is not None:
                current.raw_lines = current_lines
                transactions.append(current)
            current = None
            current_lines = []

        # Current-format rows only give MM/DD (no year); anchor to the
        # statement's own period rather than today's date.
        last_date: date | None = meta.period_start
        for line, columns in all_rows:
            date_str = _bucket(line, columns["date"]).strip()
            category_str = _bucket(line, columns["category"]).strip()
            action_str = _bucket(line, columns["action"]).strip()
            symbol_str = _bucket(line, columns["symbol"]).strip()
            desc_str = _bucket(line, columns["description"]).strip()
            qty_str = _bucket(line, columns["quantity"]).strip()
            price_str = _bucket(line, columns["price"]).strip()
            charges_str = _bucket(line, columns["charges"]).strip()
            amount_str = _bucket(line, columns["amount"]).strip()
            realized_str = _bucket(line, columns["realized"]).strip()

            is_date_line = bool(DATE_RE.match(date_str))
            # Schwab occasionally renders Category+Action glued with no space
            # (e.g. "RedemptionCash-In-Lieu"); detect a known category prefix.
            if category_str not in CATEGORIES:
                for cat in CATEGORIES:
                    if category_str.startswith(cat) and (
                        len(category_str) == len(cat) or category_str[len(cat)].isupper()
                    ):
                        remainder = category_str[len(cat) :]
                        if remainder and not action_str:
                            action_str = remainder
                        category_str = cat
                        break
            is_new_txn = category_str in CATEGORIES

            if is_date_line:
                mm, dd = date_str.split("/")
                ref = last_date or date.today()
                year = ref.year
                try:
                    txn_date = date(year, int(mm), int(dd))
                except ValueError:
                    txn_date = None
                if txn_date:
                    last_date = txn_date

            if is_new_txn:
                flush()
                current = TxnRow(
                    date=last_date,
                    category=category_str or None,
                    action=action_str or None,
                    symbol_cusip=symbol_str or None,
                    description=desc_str,
                    quantity=_qty(qty_str) if qty_str else None,
                    price=_money(price_str) if price_str else None,
                    charges=_money(charges_str) if charges_str else None,
                    amount=_money(amount_str) if amount_str else None,
                    realized_gain=_money(realized_str) if realized_str else None,
                )
                current_lines = [" ".join(w["text"] for w in line)]
            else:
                if current is None:
                    current = TxnRow(
                        date=last_date,
                        category=category_str or None,
                        action=action_str or None,
                        symbol_cusip=symbol_str or None,
                        description="",
                        quantity=None,
                        price=None,
                        charges=None,
                        amount=None,
                        realized_gain=None,
                    )
                    current_lines = []
                if action_str and not current.action:
                    current.action = action_str
                if symbol_str:
                    current.symbol_cusip = (current.symbol_cusip or "") + symbol_str
                if desc_str:
                    current.description = (current.description + " " + desc_str).strip()
                if qty_str and current.quantity is None:
                    current.quantity = _qty(qty_str)
                if price_str and current.price is None:
                    current.price = _money(price_str)
                if charges_str and current.charges is None:
                    current.charges = _money(charges_str)
                if amount_str and current.amount is None:
                    current.amount = _money(amount_str)
                if realized_str and current.realized_gain is None:
                    current.realized_gain = _money(realized_str)
                current_lines.append(" ".join(w["text"] for w in line))
        flush()

    return ParsedStatement(meta=meta, transactions=transactions, summary=summary)
