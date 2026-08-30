"""Pure parser for the OKW-style trade-tracking workbook.

Layout discovered from the real `OKW_2026.xlsx` (`TRADES 2026` / `ROTH TRADES
2026` sheets): each trade occupies one *value column* (not a row) — column
B holds the row's field label for the whole sheet, and each trade's actual
values live in whatever column its row-1 header cell is in (e.g. `E`, `G`,
`I`, ... — spacing between trades is **not** reliably 2 columns apart, so
block columns are discovered by scanning row 1 for non-empty cells, not
assumed from a fixed stride).

Deliberately has zero DB/SQLAlchemy dependency — this module only turns a
worksheet into `ParsedTrade` records. `scripts/import_historical.py` (CLI)
and `app/api/import_data.py` (staging endpoint) both call this, then handle
persistence/idempotency separately. Keeping this pure makes it cheap to unit
test against a small synthetic workbook instead of the real, private
spreadsheet file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from openpyxl.worksheet.worksheet import Worksheet

# Row-3..90 field labels, as they appear literally in column B. Matched
# case-insensitively with whitespace collapsed, since the same label has
# been observed with inconsistent leading/trailing spaces across sheets
# (e.g. `"EXPIRATION DATE "` vs `"EXPIRATION DATE"`).
LABELS = {
    "trade_date": "TRADE DATE",
    "underlying": "UNDERLYING",
    "price": "PRICE",
    "dte": "DAYS TO EXPIRATION (DTE)",
    "expiration_date": "EXPIRATION DATE",
    "short_strike": "SHORT STRIKE",
    "long_strike": "LONG STRIKE",
    "credit_debit": "CREDIT/(DEBIT)",
    "commission_per_share": "COMMISSION (PER SHARE)",
    "net_credit_per_share": "NET CREDIT (NC)",
    "risk_capital_per_share": "RISK CAPITAL (RC)",
    "margin_percent": "MARGIN %",
    "margin_capital": "MARGIN CAPITAL",
    "arorc": "ANNUALIZED RORC (ARORC)",
    "contracts": "ACTUAL CONTRACTS",
    "shares": "SHARES",
    "result": "RESULT",
    "result_date": "RESULT DATE (CLOSED OR EXPIRED)",
    "closing_debit": "CLOSING DEBIT",
    "total_debit": "TOTAL DEBIT",
    "notes": "NOTES",
}

# RESULT cell text -> trade_events.event_type. Anything not in this map
# (including blank/None, meaning still open) is left unmapped — the caller
# decides what an unrecognized non-blank RESULT means (flag for review,
# never guess).
RESULT_TO_EVENT_TYPE = {
    "EXPIRED": "EXPIRE",
    "ASSIGNED": "ASSIGN",
    "CLOSED": "CLOSE",
    "ROLLED": "ROLL",
    "ROLL": "ROLL",
}

# RESULT values that explicitly mean "still open" — recognized, but
# intentionally produce no closing event_type and no review flag.
OPEN_RESULT_VALUES = {"OPEN", ""}

FIRST_DATA_COLUMN = 5  # column E — trade blocks never start before this


def _normalize_label(text: str) -> str:
    return " ".join(text.split()).upper()


@dataclass
class ParsedTrade:
    """One column-block's worth of data, before any DB lookups.

    `ticker` and `trade_type_name` are raw strings — matching them to
    `tickers`/`trade_types` rows (get-or-create / lookup) is the loader's
    job, not the parser's, since that requires a DB session.
    """

    sheet_name: str
    column_letter: str
    ticker: str
    trade_type_name: str
    trade_date: date | None
    price_per_share: Decimal | None
    days_to_expiration: int | None
    expiration_date: date | None
    short_strike: Decimal | None
    long_strike: Decimal | None
    credit_debit: Decimal | None
    commission_per_share: Decimal | None
    net_credit_per_share: Decimal | None
    risk_capital_per_share: Decimal | None
    margin_percent: Decimal | None
    margin_capital: Decimal | None
    arorc: Decimal | None
    num_of_contracts: int | None
    num_of_shares: int | None
    result_raw: str | None
    result_date: date | None
    closing_debit: Decimal | None
    total_debit: Decimal | None
    notes: str | None
    warnings: list[str] = field(default_factory=list)

    @property
    def event_type(self) -> str | None:
        """`trade_events.event_type` for the closing event, or `None` if
        the trade is still open (blank RESULT) or the RESULT text isn't
        recognized (see `warnings` in that case — never guess silently).
        """
        if not self.result_raw:
            return None
        return RESULT_TO_EVENT_TYPE.get(self.result_raw.strip().upper())

    @property
    def needs_review(self) -> bool:
        return bool(self.warnings)


def _to_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, str) and not value.strip().replace(".", "").replace(
        "-", ""
    ).isdigit():
        # Non-numeric placeholders like "COVERED" (margin fields on covered
        # calls) or "-" (probability not yet computed) — not an error, just
        # not a number to carry into a NUMERIC column.
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _to_int(value: object) -> int | None:
    dec = _to_decimal(value)
    return int(dec) if dec is not None else None


def _to_date(value: object) -> date | None:
    if value is None or value == "":
        return None
    if hasattr(value, "date"):  # datetime -> date
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _build_label_row_map(ws: Worksheet) -> dict[str, int]:
    """Scan column B for each row's label, keyed by our normalized field
    names (`LABELS` above). Scanning per-sheet (rather than hardcoding row
    numbers) means a future sheet with shifted rows still parses correctly.

    Uses `enumerate`/plain indices rather than `cell.row`/`cell.column`,
    since read-only worksheets return lightweight `EmptyCell` placeholders
    for blank cells that don't carry those attributes.
    """
    wanted = {_normalize_label(v): k for k, v in LABELS.items()}
    found: dict[str, int] = {}
    for row_num, row_cells in enumerate(
        ws.iter_rows(min_col=2, max_col=2, max_row=200), start=1
    ):
        value = row_cells[0].value
        if value is None or not isinstance(value, str):
            continue
        key = wanted.get(_normalize_label(value))
        if key and key not in found:
            found[key] = row_num
    return found


def _find_block_columns(ws: Worksheet) -> list[int]:
    """Every column with a non-empty row-1 cell at or after column E is the
    start of one trade's data. Spacing between blocks is irregular in the
    real workbook, so this can't assume a fixed stride.
    """
    columns = []
    header_row = next(ws.iter_rows(min_row=1, max_row=1))
    for col_num, cell in enumerate(header_row, start=1):
        if col_num >= FIRST_DATA_COLUMN and cell.value not in (None, ""):
            columns.append(col_num)
    return columns


def parse_trade_sheet(ws: Worksheet) -> list[ParsedTrade]:
    """Parse every trade block in one sheet (e.g. `TRADES 2026`)."""
    label_rows = _build_label_row_map(ws)
    missing = [k for k in ("trade_date", "underlying", "price") if k not in label_rows]
    if missing:
        raise ValueError(
            f"Sheet '{ws.title}' is missing expected row labels {missing} in column "
            "B — this doesn't look like an OKW trade sheet, or its layout has "
            "changed enough that LABELS in okw_parser.py needs updating."
        )

    def get(col: int, key: str) -> object:
        row = label_rows.get(key)
        if row is None:
            return None
        return ws.cell(row=row, column=col).value

    trades: list[ParsedTrade] = []
    for col in _find_block_columns(ws):
        header_cell = ws.cell(row=1, column=col)
        header_text = str(header_cell.value).strip()

        # Ticker/type come from the row-1 header ("SLV ROCT PUT" -> "SLV" +
        # "ROCT PUT"), per how this workbook is actually filled in. The
        # UNDERLYING row (row 4) is only a cross-check, not the source of
        # truth — it's been observed to contain extra typed-in text (e.g.
        # "CMG ROCS" instead of "CMG") on some spread trades.
        header_parts = header_text.split(" ", 1)
        ticker = header_parts[0].strip()
        trade_type_name = header_parts[1].strip() if len(header_parts) > 1 else ""

        warnings: list[str] = []
        if not ticker:
            warnings.append(f"No header text found for column {header_cell.coordinate}")
        underlying = str(get(col, "underlying") or "").strip()
        if underlying and not underlying.upper().startswith(ticker.upper()):
            warnings.append(
                f"UNDERLYING ({underlying!r}) doesn't match header ticker ({ticker!r})"
            )
        result_raw = get(col, "result")
        result_text = str(result_raw).strip() if result_raw not in (None, "") else None
        if (
            result_text
            and result_text.upper() not in RESULT_TO_EVENT_TYPE
            and result_text.upper() not in OPEN_RESULT_VALUES
        ):
            warnings.append(f"Unrecognized RESULT value: {result_text!r}")

        trades.append(
            ParsedTrade(
                sheet_name=ws.title,
                column_letter=header_cell.column_letter,
                ticker=ticker,
                trade_type_name=trade_type_name,
                trade_date=_to_date(get(col, "trade_date")),
                price_per_share=_to_decimal(get(col, "price")),
                days_to_expiration=_to_int(get(col, "dte")),
                expiration_date=_to_date(get(col, "expiration_date")),
                short_strike=_to_decimal(get(col, "short_strike")),
                long_strike=(
                    _to_decimal(get(col, "long_strike"))
                    if "SPREAD" in trade_type_name.upper()
                    else None
                ),
                credit_debit=_to_decimal(get(col, "credit_debit")),
                commission_per_share=_to_decimal(get(col, "commission_per_share")),
                net_credit_per_share=_to_decimal(get(col, "net_credit_per_share")),
                risk_capital_per_share=_to_decimal(get(col, "risk_capital_per_share")),
                margin_percent=_to_decimal(get(col, "margin_percent")),
                margin_capital=_to_decimal(get(col, "margin_capital")),
                arorc=_to_decimal(get(col, "arorc")),
                num_of_contracts=_to_int(get(col, "contracts")),
                num_of_shares=_to_int(get(col, "shares")),
                result_raw=result_text,
                result_date=_to_date(get(col, "result_date")),
                closing_debit=_to_decimal(get(col, "closing_debit")),
                total_debit=_to_decimal(get(col, "total_debit")),
                notes=(str(n) if (n := get(col, "notes")) not in (None, "") else None),
                warnings=warnings,
            )
        )
    return trades
