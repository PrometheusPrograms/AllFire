# AGENTS.md — condensed cheat-sheet

Read this instead of re-scanning the repo. Only re-read the files linked below when a
prompt is specifically about that area. Update this file when the *shape* of things
changes (new table, new major convention) — not for routine feature work.

## Stack
FastAPI (`backend/`) + Next.js App Router (`frontend/`) + Postgres/Neon. Money math is
always `Decimal`/`NUMERIC`, never `float`. Migrations only through Alembic, never a
manual `ALTER TABLE`. Full detail: `docs/ARCHITECTURE.md`, `docs/DATA_MODEL.md`.

## Schema (tables — see `backend/app/models/*.py` for exact columns)
- `accounts` — e.g. "Rule 1", "Roth".
- `tickers` — get-or-create by symbol.
- `trade_types` — catalog of strategies (`ROCT PUT`, `RULE ONE PUT`, `ROCT CALL`,
  `RULE ONE CALL`, `ROCS BULL PUT SPREAD`). `ROCT *` = trading shares (short-term,
  option-assignment-driven). `RULE ONE *` = long-term investment shares (`RULE ONE CALL`
  sells long-term shares at/above full intrinsic value). This trading-vs-long-term split
  matters for the future portfolio view (Phase 5) — don't erase it when touching this data.
- `trade_types` also has `BTO`/`STC` — plain stock buy/sell (no options), feeds cost basis
  directly. All 7 types are seeded via `alembic/versions/32c3020a1e5a_seed_trade_types.py`.
- `trades` — what you decided to do, written once, **never updated after insert**. Has an
  `import_batch` column (nullable, bookkeeping only — see PRODUCTION_IMPORT_RUNBOOK.md §4).
- `trade_events` — append-only lifecycle log (`OPEN/ROLL/ADJUST/CLOSE/EXPIRE/ASSIGN`).
  Current status is *derived*, never stored — see `v_trade_current_status` view. Also has
  `import_batch`.
- `cash_flows`, `cost_basis` (running totals via `v_cost_basis_running` view, computed at
  query time — never stored), `bankroll`, `commissions`. `cost_basis` is populated by
  `scripts/backfill_cost_basis.py` (and, later, live writes): BTO/STC plus ASSIGN on
  `ROCT`/`RULE ONE` PUT/CALL. **Bull put spreads never write `cost_basis`** — they
  cannot be assigned. A closed `ROCS BULL PUT SPREAD` converts to a sibling
  `ROCT PUT` or `RULE ONE PUT` (same account/ticker/expiration/short strike, opened
  on the close date); that PUT is what may later ASSIGN.

## Non-negotiable conventions
1. `trades` rows are immutable after insert. Every lifecycle change is a new
   `trade_events` row.
2. `Decimal` in Python, `NUMERIC` in Postgres, everywhere money/price/ratio touches code.
3. Alembic migrations only, expand/contract pattern for anything destructive (mark
   destructive migrations `# BREAKING: <reason>`).
4. Staging before prod, always: feature branch → staging → manual verification → main.
5. Env-gated features (e.g. spreadsheet import) exist only where the env var is set —
   genuinely absent from prod's route table, not just hidden.
6. Pure calculation logic lives in `backend/app/services/*.py`, tested in isolation
   before any route/UI touches it.

## Key files
- `backend/app/main.py` — router registration (routers are only wired in here).
- `backend/app/api/*.py` — HTTP routes, one module per resource.
- `backend/app/services/{kelly,rorc,cost_basis,premium,position}.py` — pure, tested financial calculations.
- `backend/scripts/` — one-off/CLI data tooling, never imported by the running app except
  `import_data.py` reusing it:
  - `okw_parser.py` — pure parser for the real OKW workbook layout (column-block-per-trade,
    labels in column B). Zero DB dependency, unit-tested with synthetic workbooks.
  - `okw_loader.py` — turns `ParsedTrade`s into `trades`/`trade_events` rows; owns
    get-or-create + `import_batch` idempotency (refuses a re-run unless `force=True`).
  - `xlsx_compat.py` — the real workbook is OOXML **Strict** conformance (not the usual
    Transitional) plus has stale external-link refs and a few out-of-range font values;
    this patches the container XML before handing off to openpyxl. `load_workbook_safely()`
    is a drop-in for `openpyxl.load_workbook()` everywhere this file might be opened.
  - `import_historical.py` — the CLI from PRODUCTION_IMPORT_RUNBOOK.md
    (`--account rule1|roth --year YYYY --file ...`).
  - `backfill_cost_basis.py` — one-time (and re-runnable) derivation of
    `cost_basis` from BTO/STC and ASSIGN events. Spreads are excluded.
- `backend/app/api/import_data.py` — staging-only `POST /api/import/spreadsheet`
  (upload-based), gated by `ENABLE_SPREADSHEET_IMPORT`, reuses the same parser/loader.
- `frontend/lib/api-client.ts` — typed fetch wrappers; all money fields are strings
  end-to-end (backend serializes `Decimal` as string; never round-trip through JS `number`).
- `docs/PRODUCTION_IMPORT_RUNBOOK.md` — the *only* sanctioned path for real historical
  data into production.

## Local dev DB
`backend/.env`'s `DATABASE_URL` points at a real Postgres (Neon) instance — treat it as
live data, not a disposable sandbox. For throwaway local testing (migrations, parser
dry-runs), override with `DATABASE_URL=sqlite:///./dev_local.db` rather than assuming
the ambient `.env` is safe to run destructive commands against.
