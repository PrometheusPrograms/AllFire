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
  OKW column cells `DELTA`, Prob OTM (`probability_of_winning`), `FINAL ARORC`,
  and `NET CREDIT/(DEBIT)` (`result_net_credit`) live on the trade. Opening
  `arorc` is the in-trade ARORC row; `final_arorc` is the result-block cell.
  Omitted on early imports; filled by `scripts/backfill_okw_column_fields.py`
  (nulls only — not a lifecycle rewrite).
- `trade_events` — append-only lifecycle log (`OPEN/ROLL/ADJUST/CLOSE/EXPIRE/ASSIGN`).
  Current status is *derived*, never stored — see `v_trade_current_status` view. Also has
  `import_batch`. `trade_parent_id` links an OKW roll continuation (the next
  column) to the rolled trade; each column stays its own immutable row. The
  trades list collapses a chain to one row; the detail timeline still links
  each event to that leg. Backfill: `scripts/link_roll_parents.py`.
- `cash_flows` (dividends from the Schwab CLI use `transaction_type='DIVIDEND'` and
  `schwab_activity_id`), `cost_basis` (running totals via `v_cost_basis_running` view,
  computed at query time — never stored), `bankroll`, `commissions`. `cost_basis` is
  populated by `scripts/backfill_cost_basis.py` (and live writes from Schwab BTO import):
  BTO/STC plus ASSIGN on
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
   **Code and schema** move that way; **rows do not.** Never dump `dev` into staging/prod.
   See `docs/ENVIRONMENTS.md`.
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
  - `cleanup_assign_duplicates.py` — deletes Schwab BTO/STC that duplicate OKW ASSIGN lots.
  - `okw_parser.py` — pure parser for the real OKW workbook layout (column-block-per-trade,
    labels in column B). Zero DB dependency, unit-tested with synthetic workbooks.
  - `okw_loader.py` — turns `ParsedTrade`s into `trades`/`trade_events` rows; owns
    get-or-create + `import_batch` idempotency (refuses a re-run unless `force=True`).
    Adjacent RESULT=ROLL columns set `trade_parent_id`.
  - `link_roll_parents.py` — sets `trade_parent_id` on leftover OKW roll
    continuations (re-runnable; also called at the end of each load).
  - `xlsx_compat.py` — the real workbook is OOXML **Strict** conformance (not the usual
    Transitional) plus has stale external-link refs and a few out-of-range font values;
    this patches the container XML before handing off to openpyxl. `load_workbook_safely()`
    is a drop-in for `openpyxl.load_workbook()` everywhere this file might be opened.
  - `import_historical.py` — the CLI from PRODUCTION_IMPORT_RUNBOOK.md
    (`--account rule1|roth --year YYYY --file ...`).
  - `schwab_parser.py` / `schwab_loader.py` / `import_schwab.py` — Schwab Trader API
    gap-fill: cash dividends → `cash_flows`, equity BUY/SELL → `BTO`/`STC` +
    `cost_basis`. Assignment/exercise legs are left to the OKW `trade_events`
    ASSIGN flow, not written here. Auth is `schwab_auth.py` (token file
    gitignored). Docs: `docs/SCHWAB_IMPORT.md`.
  - `manual_entry.py` — JSON/CSV for hand-typed rows Schwab can't classify
    (interest, DRIP, assignment-adjacent, pre-Schwab-window) (`import_batch`
    default `manual`). Account 641: `scripts/data/acct641_manual_entries.json`
    (`acct641_migration`) plus `_load_641_options.py`. Pre-API / under-counted
    lots from PDFs: `scripts/data/statement_gap_lots.json` (Roth TSLA/CWT
    `acct467_roth_history`, Rule 1 OXY `oxy_stmt_catchup`). LULU Rule 1 9-vs-10
    share API under-count: `repair_lulu_truncated_btos.py`. GOOG/GOOGL 20:1
    split restated onto the 2014 lot: `repair_641_split_lots.py`.
  - `validate_import.py` — read-only promotion gate: recompute ARORC/premium/cost
    basis, structural checks. Must exit 0 on the target DB before staging/prod.
  - `reconcile_statements.py` — read-only OKW/`cost_basis` vs Schwab PDF
    transactions (statements are fill ground truth). LULU Rule 1 expected
    lots: `scripts/data/lulu_rule1_expected_lots.json` (900 sh).
  - `backfill_expire_event_dates.py` — one-shot: EXPIRE `event_date` =
    `trades.expiration_date` (fixes rows imported before the loader used
    expiration for EXPIRE).
  - `backfill_okw_column_fields.py` — one-shot: fill null `delta` /
    `probability_of_winning` / `final_arorc` / `result_net_credit` from the
    matching OKW column.
  - `backfill_cost_basis.py` — one-time (and re-runnable) derivation of
    `cost_basis` from BTO/STC and ASSIGN events. Spreads are excluded.
- `backend/app/api/import_data.py` — staging-only `POST /api/import/spreadsheet`
  (upload-based), gated by `ENABLE_SPREADSHEET_IMPORT`, reuses the same parser/loader.
- `frontend/lib/api-client.ts` — typed fetch wrappers; all money fields are strings
  end-to-end (backend serializes `Decimal` as string; never round-trip through JS `number`).
- `docs/PRODUCTION_IMPORT_RUNBOOK.md` — the *only* sanctioned path for real historical
  data into production.
- `docs/ENVIRONMENTS.md` — Neon `dev` / `staging` / `production`: what data belongs
  where, reset-from-parent, never promote test trades.

## Local / Neon DBs
`backend/.env`'s `DATABASE_URL` should point at Neon branch **`dev`** — a real remote
Postgres, but a **scratch pad**. Test trades and experimental Schwab imports stay there;
they never appear on staging or production unless you dump/restore (don't) or reset a
child from production. For SQLite throwaways (migrations, parser dry-runs), override
with `DATABASE_URL=sqlite:///./dev_local.db`. Never point local `.env` at production.
When `dev` is too dirty, reset it from parent `production` (see ENVIRONMENTS.md).
