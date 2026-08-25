# Production Import Runbook

**Purpose:** one-time seeding of the live database from historical spreadsheets, once
development/testing is complete. Not a repeatable app feature — see `ARCHITECTURE.md` §6a
for why the import UI is env-gated to staging only and doesn't exist in production.

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

## 3. Rehearsal (mandatory before touching prod)

1. Create a Neon branch off the current production database (same instant branch-off used
   for pre-deploy snapshots in `CI_CD.md`).
2. Run the **full six-import sequence**, in order, against that branch — not just one file
   in isolation. The cross-year linkage and bankroll reconciliation bugs only show up once
   multiple years are chained together.
3. Validate on the rehearsal branch:
   - Row counts match each spreadsheet's trade count.
   - Sum of `net_credit` per year reconciles with that spreadsheet's own YTD summary total.
   - Spot-check calculated RORC/ARORC/cost-basis on a handful of trades per year against the
     spreadsheet's own numbers.
   - Bankroll reconciliation per §2c passes at every year boundary, both accounts.
   - No unresolved "suspected boundary-spanning trade" flags from §2b.
4. Fix, re-rehearse, repeat until clean. Delete the rehearsal branch once satisfied.

## 4. Idempotency guard

The `import_batch` column (e.g. `"rule1_2025"`) is bookkeeping, not part of the domain
model — purely additive, fits the expand/contract migration pattern. It's what makes
"re-run just 2025 for Rule 1 without touching the other five files" possible, and what lets
the script refuse an accidental double-run against a live account.

## 5. Running it for real

1. Point the script at production's actual `DATABASE_URL`.
2. Run all six imports, in order, exactly as rehearsed.
3. Re-run the full validation checklist from §3.3 against production itself.
4. Manually trigger the backup workflow (`workflow_dispatch` on `backup.yml`) immediately —
   don't wait for the nightly schedule. You want an encrypted copy in Backblaze B2 within
   minutes of this data existing.
5. Clear the script's prod `DATABASE_URL`/credentials from shell history and any local env
   file. It only needed to exist for this one operation.
6. Confirm (again) that `ENABLE_SPREADSHEET_IMPORT` is unset on the production Render
   service — this runbook is the only path real historical data should ever take into prod.
