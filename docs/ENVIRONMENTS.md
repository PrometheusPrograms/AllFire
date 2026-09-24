# Test vs staging vs production data

Staging is **not** a database you later push into production.

- **Git** (`feature` → `staging` → `main`) moves **code**.
- **Alembic** moves **schema**, applied separately on each Neon branch.
- **Rows** (trades, events, cash flows) live on whichever Neon branch you wrote them to.
  Neon copy-on-write does **not** promote child writes up to the parent.

## Neon layout (project Fire)

Project `shiny-breeze-57839465`. Branch **production** (`br-wild-lake-avfda1co`) is
primary. **dev** and **staging** are siblings, both parented on production.

```
production
  ├── staging
  └── dev
```

Local `backend/.env` `DATABASE_URL` should point at **dev** (hostname
`ep-hidden-forest-…`). Writes there never appear on staging or production unless you
explicitly dump/restore or reset a branch.

## What belongs where

| Layer | Dev | Staging | Production |
|---|---|---|---|
| Purpose | Break things: fake trades, Schwab dry-runs, UI experiments | Dress rehearsal of **real** data + real app | Canonical ledger |
| Data source | Anything; disposable | Same **sanctioned** imports as prod ([PRODUCTION_IMPORT_RUNBOOK.md](PRODUCTION_IMPORT_RUNBOOK.md), [SCHWAB_IMPORT.md](SCHWAB_IMPORT.md)) | Those CLIs only, after rehearsal |
| How it gets prod-like | Optional: **reset from parent** when you want a fresh copy of production | Reset from parent, **or** run the same import sequence against staging | Never reset from a child |

[CI_CD.md](CI_CD.md) and [ARCHITECTURE.md](ARCHITECTURE.md) §4: staging is a copy-on-write
clone of prod so you can verify the **app** against realistic data without touching
production. It is not a pipeline that copies the playground.

## Day-to-day process

1. **Develop on `dev`.** Add test trades freely. Schwab OAuth plus `--dry-run` or a real
   import against the **dev** `DATABASE_URL` is fine if you accept those rows only exist
   on `dev`.
2. **Ship code** with a PR into git `staging`, then a second PR `staging` → `main`.
   Migrations run on the **target** environment’s `DATABASE_URL`, not by copying tables
   from `dev`.
3. **Do not** `pg_dump` dev → restore staging. That is how test trades leak.
4. **When you want real history on staging** (the dataset you will later load on
   production):
   - Prefer: reset Neon `staging` from parent `production` (wipe staging-only rows, copy
     current prod), **or** run the OKW/Schwab CLIs against staging’s URL if prod is still
     empty or incomplete.
   - Rehearse the full import on staging (or a throwaway branch off production) per the
     production runbook §3, then run the **same** commands against production.
5. **When `dev` is too dirty:** reset `dev` from parent `production`. You lose test
   trades; you get a clean clone of prod again. Recreate experiments as needed.

Reset-from-parent is a Neon console/CLI operation (`reset_from_parent` / `neon branches
reset`). It is **destructive on the child only**. Never reset `production` from `dev` or
`staging`.

## Schwab / OKW

Those importers write to whatever `--database-url` / `DATABASE_URL` you pass. They do
not sync environments.

- Experiments and “does the parser work?” → **dev**
- “This is the ledger I will live with” → run against **staging**, then **production**,
  with the same files/date ranges, only after `scripts.validate_import` exits 0
  on `dev` (see [PRODUCTION_IMPORT_RUNBOOK.md](PRODUCTION_IMPORT_RUNBOOK.md) §3)
- Idempotency is per activity id / `import_batch` **inside that database**, not across
  branches

## Practical rule

Treat **production** as the source of truth for real money data. Treat **staging** as a
disposable clone of that truth for verifying deploys. Treat **dev** as a scratch pad.
Data flows **down** (prod → reset child) or **sideways via CLI import**, never **up**
from test trades.
