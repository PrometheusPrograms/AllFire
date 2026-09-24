# Production Import Runbook

**Purpose:** one-time seeding of the live database from historical spreadsheets, once
development/testing is complete. Not a repeatable app feature — see `ARCHITECTURE.md` §6a
for why the import UI is env-gated to staging only and doesn't exist in production.

Do **not** copy rows from Neon `dev` (test trades) into staging or production. Rehearse
the CLI against staging or a throwaway branch of production, then run the same commands
against production. See [ENVIRONMENTS.md](ENVIRONMENTS.md).

**Scope:** 2 accounts (`Rule 1`, `Roth`), each with one spreadsheet per year (3+ years each,
6+ files total). Imports must run **chronologically, per account** — this is a correctness
requirement, not a preference. See §2.

## 1. Script, not endpoint

Reuse the parsing/mapping logic from the staging import feature, wrapped as a CLI script
that talks directly to a `DATABASE_URL` you supply — never through the running app's API,
staging or prod:

```
python -m scripts.import_historical --account rule1 --year 2024 --file rule1_2024.xlsx
python -m scripts.import_historical --account rule1 --year 2025 --file rule1_2025.xlsx
python -m scripts.import_historical --account rule1 --year 2026 --file rule1_2026.xlsx
python -m scripts.import_historical --account roth   --year 2024 --file roth_2024.xlsx
python -m scripts.import_historical --account roth   --year 2025 --file roth_2025.xlsx
python -m scripts.import_historical --account roth   --year 2026 --file roth_2026.xlsx
```

Each run should:
- Tag every inserted row with an `import_batch` value (e.g. `"rule1_2025"`) — see §4.
- Refuse to run again for a batch that already exists, unless `--force` is explicitly passed.

## 2. Why order matters (not just tidiness)

**a) Tickers must be get-or-create.** The same ticker (e.g. CELH) will appear across
multiple years' files. Before inserting a `tickers` row, look up by `symbol` first and reuse
the existing `id`. Creating a duplicate splits that ticker's trades across two rows and
breaks every ticker-level rollup.

**b) Trades spanning a year boundary need to be matched, not duplicated.** A trade opened
in December of year N and rolled or closed in January of year N+1 will appear once at the
end of year N's file and again at the start of year N+1's. Before inserting a new `trades`
row, check for an existing match on
`(account_id, ticker_id, strike_price, expiration_date, date_trade_open)`. If found, the
year N+1 file's entry should produce a `trade_events` row (ROLL/CLOSE/etc.) against the
*existing* trade, not a new `trades` row. Flag any suspected boundary-spanning trade for
manual review rather than guessing silently.

**c) Bankroll must reconcile across the boundary.** Each spreadsheet reports its own
"bankroll start of year." In the database, bankroll is continuous per account, not reset
per file. After each year's import, compare the account's running bankroll total (from
`v_ytd_summary` or equivalent) against the *next* file's stated starting bankroll. A
mismatch means something in the year you just imported didn't land right — catch it before
stacking another year on top of the error.

## 3. Clean-before-live pipeline

Import **into Neon `dev` first**, work the review queue, and only promote when
`python -m scripts.validate_import` exits 0. Never dump `dev` into staging/prod —
re-run the same CLIs against each environment. See [ENVIRONMENTS.md](ENVIRONMENTS.md).

**Order on `dev`:**

1. `import_historical.py` per account/year, chronologically (§1).
2. `import_schwab.py` per account (`--dry-run` first) — see [SCHWAB_IMPORT.md](SCHWAB_IMPORT.md).
   This writes dividends, BTO, **and** STC directly; only rows Schwab can't
   classify land on its `review` list. If a prior Schwab run wrote assignment
   equity legs as BTO/STC, repair with
   `python -m scripts.cleanup_assign_duplicates --database-url "$DATABASE_URL"`
   (`--dry-run` first).
3. Leftover dividends / adjustments / anything on the Schwab review list via
   `scripts/manual_entry.py` (JSON or CSV). Default `import_batch` is `manual`.
   Account 641 reconstruction: `backend/scripts/data/acct641_manual_entries.json`
   plus `python -m scripts._load_641_options`. Pre-Schwab-window Roth lots and
   the Rule 1 OXY under-count:
   `backend/scripts/data/statement_gap_lots.json`.
4. Clear the review queue:
   - `needs_review=true` on `trades` (OKW ambiguous parses)
   - Schwab `review` lines printed by `import_schwab` (interest, DRIP,
     assignment/exercise, option fills, `unsupported_type:*`, incomplete
     rows) — each item is a manual row **or** an explicit skip, never a
     silent drop
5. `python -m scripts.reconcile_statements --database-url "$DATABASE_URL"`
   `--statements-dir … --account rule1 --ticker LULU` (then other tickers).
   Statements are fill ground truth; OKW is the strategy log. Exit 0 (or a
   documented exception list). Rule 1 LULU expected remaining shares are 900
   (`scripts/data/lulu_rule1_expected_lots.json`).
6. `python -m scripts.validate_import --database-url "$DATABASE_URL"`
   (`--expected expected.json` when you have spreadsheet year totals). Exit 0 is
   the gate. Re-import or reset `dev` from production and retry until it is.

If EXPIRE events still share the trade's open date, run
`python -m scripts.backfill_expire_event_dates --database-url "$DATABASE_URL"`
(`--dry-run` first) instead of `--force` re-importing whole OKW years.

If `trades.delta` / `probability_of_winning` / `final_arorc` /
`result_net_credit` are still null, run
`python -m scripts.backfill_okw_column_fields --database-url "$DATABASE_URL"
--account rule1 --year 2026 --file …` (and the Roth / other-year files). It only
fills nulls from the matching OKW column. Use a current workbook (cached formula
values). New OKW imports write those fields on insert.

If `trades.trade_parent_id` is still null on historical rolls, run
`python -m scripts.link_roll_parents --database-url "$DATABASE_URL"`
(`--dry-run` first). New OKW imports set it during load. The UI treats a
linked chain as one trade (OKW left-to-right columns); the DB still stores
each column as its own row.

`validate_import.py` is read-only. It recomputes net credit / risk capital /
ARORC from stored inputs (`app.services.rorc`), walks `cost_basis` the same way
`v_cost_basis_running` does, sums premium (`app.services.premium`) per
account/year, reconciles year-start bankroll when `--expected` is given, and
flags leftover `needs_review`, duplicate option keys (same type, contract count,
and credit), and orphan FKs. Zero-cost split lots, share-decreasing sales, and assignment-acquired lots
(description starts with `Assigned`) are
not treated as implausible basis swings. Inventory gaps that the 2013–2026
statement PDFs resolve (Roth TSLA original + splits, Roth CWT original + DRIP
catch-up, Rule 1 OXY 2026-04-20 436-share remainder) live in
`scripts/data/statement_gap_lots.json` and should be loaded before treating
negative running-shares as residual.

Empty production/staging also need accounts before any import:

```
python -m scripts.seed_accounts --database-url "$DATABASE_URL"
```
flags leftover `needs_review`, duplicate option keys, and orphan FKs.

`--expected` JSON:

```json
{
  "premium": [{"account": "Rule 1", "year": 2025, "total": "12345.67"}],
  "bankroll_year_start": [{"account": "Rule 1", "year": 2026, "balance": "50000.00"}]
}
```

Manual rows (JSON list or CSV with the same columns):

```json
[
  {"kind": "stc", "account": "Rule 1", "ticker": "ADBE", "date": "2025-06-01",
   "shares": 100, "price": "12.50", "commission_per_share": "0.00"},
  {"kind": "dividend", "account": "Rule 1", "ticker": "AAPL", "date": "2025-03-15",
   "amount": "12.34"}
]
```

```
python -m scripts.manual_entry --database-url "$DATABASE_URL" --file rows.json [--dry-run]
python -m scripts.validate_import --database-url "$DATABASE_URL" [--expected expected.json]
```

**Rehearsal (mandatory before touching prod):**

1. Create a Neon branch off the current production database (same instant branch-off used
   for pre-deploy snapshots in `CI_CD.md`), **or** use the long-lived `staging` branch
   after resetting it from parent production — see [ENVIRONMENTS.md](ENVIRONMENTS.md).
   Do not rehearse by restoring a dump of `dev`.
2. Run the **full import sequence** (OKW → Schwab → manual_entry), in order, against that
   branch — not just one file in isolation. The cross-year linkage and bankroll
   reconciliation bugs only show up once multiple years are chained together.
3. `validate_import.py` must exit 0 on that branch. Also confirm:
   - Row counts match each spreadsheet's trade count.
   - No unresolved Schwab `review` items you meant to import.
4. Fix, re-rehearse, repeat until clean. Delete a throwaway rehearsal branch once satisfied.

## 4. Idempotency guard

The `import_batch` column (e.g. `"rule1_2025"`) is bookkeeping, not part of the domain
model — purely additive, fits the expand/contract migration pattern. It's what makes
"re-run just 2025 for Rule 1 without touching the other five files" possible, and what lets
the script refuse an accidental double-run against a live account.

## 5. Running it for real

1. Point the script at production's actual `DATABASE_URL`.
2. Run all six imports, in order, exactly as rehearsed.
3. Re-run `python -m scripts.validate_import --database-url <prod>` (same
   `--expected` file as rehearsal). Do not treat production as live until this
   exits 0.
4. Manually trigger the backup workflow (`workflow_dispatch` on `backup.yml`) immediately —
   don't wait for the nightly schedule. You want an encrypted copy in Backblaze B2 within
   minutes of this data existing.
5. Clear the script's prod `DATABASE_URL`/credentials from shell history and any local env
   file. It only needed to exist for this one operation.
6. Confirm (again) that `ENABLE_SPREADSHEET_IMPORT` is unset on the production Render
   service — this runbook is the only path real historical data should ever take into prod.

## 6. Schwab gap-fill (dividends, BTO, and STC)

OKW workbooks do not record cash dividends or many outright share buys/sells.
Those are imported separately via the Schwab CLI — see `docs/SCHWAB_IMPORT.md`.
Interest, DRIP, assignment/exercise legs, and other review-list items go
through `scripts/manual_entry.py`. That path is incremental (activity-id /
matching-row idempotency), still a script against a `DATABASE_URL`, and still
should be rehearsed on a Neon branch before production. It does **not**
re-import option trades from Schwab. `validate_import.py` must pass after the
gap-fill the same as after OKW.
