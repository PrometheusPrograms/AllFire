"""Staging-only spreadsheet import endpoint.

Env-gated by `settings.enable_spreadsheet_import` (see docs/ARCHITECTURE.md
§6a): the router is only included in `app.main` when that's true, so this
route is genuinely absent from prod's route table, not just hidden behind
an auth check. Production's real historical import goes exclusively through
`scripts/import_historical.py` per docs/PRODUCTION_IMPORT_RUNBOOK.md — this
endpoint exists purely so staging can iterate on the parser/loader against
a real upload without shelling into the Render instance.

Reuses the exact same `okw_parser`/`okw_loader` the CLI script uses, so
behavior can't drift between "what staging validated" and "what actually
runs against prod."
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from scripts.import_historical import ACCOUNTS
from scripts.okw_loader import ImportBatchExistsError, load_parsed_trades
from scripts.okw_parser import ParsedTrade, parse_trade_sheet
from scripts.xlsx_compat import load_workbook_safely

router = APIRouter(prefix="/api/import", tags=["import"])


class ImportWarning(BaseModel):
    column_letter: str
    ticker: str
    trade_type_name: str
    warnings: list[str]


class ImportResponse(BaseModel):
    trades_created: int
    events_created: int
    needs_review: int
    skipped_no_ticker: list[str]
    unknown_trade_types: list[str]
    review_details: list[ImportWarning]


@router.post("/spreadsheet", response_model=ImportResponse)
async def import_spreadsheet(
    account: str,
    year: int,
    force: bool = False,
    dry_run: bool = False,
    file: UploadFile = None,  # type: ignore[assignment]
    db: Session = Depends(get_db),
) -> ImportResponse:
    if account not in ACCOUNTS:
        raise HTTPException(400, f"Unknown account {account!r}. Expected one of {sorted(ACCOUNTS)}.")
    if file is None:
        raise HTTPException(400, "A .xlsx file upload is required.")

    account_name, sheet_prefix = ACCOUNTS[account]
    sheet_name = f"{sheet_prefix} {year}"
    import_batch = f"{account}_{year}"

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)

    try:
        workbook = load_workbook_safely(tmp_path, data_only=True, read_only=False)
        if sheet_name not in workbook.sheetnames:
            raise HTTPException(
                400, f"Sheet {sheet_name!r} not found. Available: {workbook.sheetnames}"
            )
        parsed_trades: list[ParsedTrade] = parse_trade_sheet(workbook[sheet_name])
    finally:
        tmp_path.unlink(missing_ok=True)

    review_details = [
        ImportWarning(
            column_letter=t.column_letter,
            ticker=t.ticker,
            trade_type_name=t.trade_type_name,
            warnings=t.warnings,
        )
        for t in parsed_trades
        if t.needs_review
    ]

    if dry_run:
        return ImportResponse(
            trades_created=0,
            events_created=0,
            needs_review=len(review_details),
            skipped_no_ticker=[],
            unknown_trade_types=sorted({t.trade_type_name for t in parsed_trades if t.needs_review}),
            review_details=review_details,
        )

    try:
        result = load_parsed_trades(
            db, parsed_trades, account_name=account_name, import_batch=import_batch, force=force
        )
    except ImportBatchExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.commit()

    return ImportResponse(
        trades_created=result.trades_created,
        events_created=result.events_created,
        needs_review=result.needs_review,
        skipped_no_ticker=result.skipped_no_ticker,
        unknown_trade_types=result.unknown_trade_types,
        review_details=review_details,
    )
