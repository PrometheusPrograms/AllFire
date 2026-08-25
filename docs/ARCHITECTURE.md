# Trade Tracker — Architecture

**Status:** Draft v1 — starting point for implementation in Cursor
**Owner:** You (solo dev), architecture reviewed by Claude, implemented via Cursor

## 1. What this app replaces

Source: `OKW_2026.xlsx` — a Rule One-style options-selling tracker (cash-secured puts,
credit spreads) with a per-trade screening checklist (STT), strike/pricing math, Kelly
Criterion position sizing, an ARORC (Annualized Return on Risk Capital) metric, bankroll
tracking, and a YTD summary. See `DATA_MODEL.md` for the full field-by-field mapping from
spreadsheet to schema.

## 2. Stack

| Layer | Choice | Notes |
|---|---|---|
| Source control / CI | GitHub + GitHub Actions | Free. Every service below deploys off pushes to this repo. |
| Database | Neon (Postgres) | Free tier, permanent, standard Postgres — portable if you ever need to move it. Branching gives free staging DBs. |
| Backend | FastAPI (Python) on Render | Free tier (cold start ~30–50s after 15 min idle — fine for single-user). Async-capable, which matters once agents/scheduled jobs arrive. |
| Frontend | Next.js (React/TypeScript) on Vercel | Free tier. Deploys a preview URL per PR automatically. |
| Auth | Self-issued JWT (email + password, bcrypt) to start | Single user today — don't pay for managed auth (Clerk/Supabase Auth) until you actually have multiple users. Swap-in point is isolated (see §7). |
| Migrations | Alembic | Every schema change is a versioned, reviewable file — never hand-edit the DB. |
| Money math | Python `Decimal`, Postgres `NUMERIC` | Never `float` for anything touching dollars, strikes, or percentages. |
| Backup storage | Backblaze B2 (free 10GB tier) | Separate from the code repo entirely — see §6.4. Nightly encrypted dumps, reachable only with a private key. |

## 3. Repo structure

```
trade-tracker/
├── backend/
│   ├── app/
│   │   ├── main.py                # FastAPI app entrypoint
│   │   ├── api/                   # route handlers, grouped by resource
│   │   │   ├── trades.py
│   │   │   ├── events.py
│   │   │   └── analytics.py
│   │   ├── models/                # SQLAlchemy models
│   │   ├── schemas/                # Pydantic request/response schemas
│   │   ├── services/               # Kelly, ARORC, cost-basis calculations — pure functions, heavily tested
│   │   ├── db.py                   # session/engine setup
│   │   └── config.py                # env var loading
│   ├── alembic/                    # migrations
│   ├── tests/                      # pytest — services/ gets the most coverage
│   ├── pyproject.toml / requirements.txt
│   └── render.yaml                 # Render service config
├── frontend/
│   ├── app/                        # Next.js app router
│   ├── components/
│   ├── lib/                        # API client, formatting helpers
│   └── package.json
├── docs/
│   ├── ARCHITECTURE.md             # this file
│   ├── DATA_MODEL.md
│   └── ROADMAP.md
├── .github/
│   └── workflows/
│       ├── backend-ci.yml          # lint + pytest on every PR
│       ├── frontend-ci.yml         # lint + build on every PR
│       └── backup.yml              # nightly pg_dump → gpg encrypt → Backblaze B2 (never GitHub)
└── README.md
```

## 4. Environments

| Env | Database | Backend | Frontend | Purpose |
|---|---|---|---|---|
| Local | Neon branch `dev` (or local Postgres via Docker) | `uvicorn` on localhost | `next dev` | Day-to-day work |
| Staging | Neon branch `staging` (free, instant) | Render PR preview | Vercel PR preview | Test a change against realistic data before it touches prod |
| Production | Neon branch `main` | Render production service | Vercel production | Real trades, real money data |

Neon's branching is the reason this environment split costs nothing extra — a staging
database is a copy-on-write branch of prod, not a second thing you have to provision or pay
for.

## 5. Deployment flow

1. Work happens on a feature branch, implemented in Cursor.
2. Push → GitHub Actions runs backend tests (pytest) and frontend build/lint.
3. Open a PR → Render and Vercel each spin up a preview environment automatically.
4. Merge to `main` → auto-deploys to production on both Render and Vercel.
5. Alembic migrations run as an explicit step in the deploy pipeline, never automatically
   against prod without a review.

## 6. Data integrity principles (non-negotiable)

These exist because this is the part of the plan protecting real trade history:

1. **Append-only ledger for trade lifecycle.** A trade's outcome is never written by
   overwriting a row — it's recorded as a new `trade_events` row (OPEN, ROLL, ADJUST, CLOSE,
   EXPIRE, ASSIGN). Current status and realized P&L are *derived* from the event history, not
   stored as a mutable field. See `DATA_MODEL.md` §1 for why this matters.
2. **All money as `NUMERIC`, never `float`,** in both Postgres and Python (`Decimal`).
   Floating-point rounding errors are unacceptable in cost-basis math.
3. **Migrations only through Alembic**, checked into git, applied through the CI pipeline —
   never a manual `ALTER TABLE` against prod.
4. **Nightly backup, independent of Neon — and never in the code repo.** A GitHub Action
   runs `pg_dump` every night, encrypts the dump (`gpg --symmetric`, passphrase stored as a
   GitHub Actions secret), and pushes it to **Backblaze B2** — a storage bucket that is
   *not* your GitHub repo and reachable only with a private B2 application key, itself
   stored as a secret. This is deliberate: a "private" GitHub repo is private only until a
   fork, a collaborator, or a leaked token changes that — it's built for sharing code, not
   for vaulting financial data. Git also isn't built to hold nightly binary dumps; its
   history only grows, never shrinks. B2's free tier (10GB) is independent of Neon, so a bad
   day at either provider doesn't take out both your live data and your backups at once, and
   the encryption means even the bucket itself never holds readable trade data.
5. **Tests around every financial calculation** (Kelly sizing, ARORC, cost basis) before any
   UI is built on top of them — these are pure functions in `services/`, testable in
   isolation, and this is exactly the kind of code worth an Opus review pass before it's
   trusted with real trades.

## 6a. Environment-gated features (staging-only tooling)

Some things — like a spreadsheet import button used to seed/validate staging data — should
exist in staging and simply not exist in production, not just be hidden or permission-gated
there.

- **Backend:** the route is only registered if an env var is set, e.g.
  `if settings.enable_spreadsheet_import: app.include_router(import_router)` in `main.py`.
  Set `ENABLE_SPREADSHEET_IMPORT=true` on the Render **staging** service only; leave it
  unset (defaults false) on production. The endpoint is genuinely absent from prod's route
  table, not just blocked.
- **Frontend:** Vercel supports different env vars per environment (Preview vs Production)
  natively. Set `NEXT_PUBLIC_ENABLE_IMPORT=true` only on staging/preview, and gate the
  button on it: `{process.env.NEXT_PUBLIC_ENABLE_IMPORT === 'true' && <ImportSpreadsheetButton />}`.
- **The import itself should be idempotent.** Tag each import run with a batch ID; a
  re-import should wipe-and-replace rows from that batch rather than duplicate them. Given
  the import doubles as the test oracle for validating UI and calculations against the
  spreadsheet's own numbers, you'll be re-running it often while iterating — it needs to be
  safe to run repeatedly without manually clearing the staging DB each time.
- **Same pattern for any future staging-only tooling** — a "reset to sample data" button,
  test-only endpoints, etc. — env-gated existence, not a permission check that could be
  bypassed or a hidden route someone stumbles onto.

## 7. Auth

Start simple: email + bcrypt-hashed password + JWT, single user, no third-party dependency.
The design keeps this behind a thin `auth/` interface so swapping in a managed provider
later (if you ever add other users) doesn't touch the rest of the app.

## 8. Division of labor (Claude ↔ Cursor)

- **Claude (here):** this doc, the data model, the Kelly/ARORC/cost-basis logic design,
  schema changes, anything security- or money-math-related, debugging design-level issues.
- **Cursor:** scaffolding the repo from this doc, writing routes/components, running tests,
  iterating on UI, day-to-day implementation.

## 9. Immediate next steps

1. Create the GitHub repo, add this `docs/` folder.
2. Sign up for Neon, Render, Vercel (GitHub SSO on all three).
3. In Cursor: scaffold `backend/` and `frontend/` per the structure above.
4. Implement the schema in `DATA_MODEL.md` as the first Alembic migration.
5. Write the Kelly/ARORC/cost-basis functions in `services/` with tests *before* any route
   or UI touches them — verify they reproduce the spreadsheet's numbers on your historical
   trades exactly.
6. Build the nightly backup GitHub Action early, not last — it should exist before real
   data does (encrypted, to Backblaze B2, per §6.4).
7. Build the env-gated spreadsheet import (§6a) against staging, and use it to load
   `OKW_2026.xlsx` — diff the app's calculated RORC/ARORC/cost-basis against the
   spreadsheet's own numbers for every imported trade before trusting the UI on anything else.
8. Confirm `ENABLE_SPREADSHEET_IMPORT` is unset on the production Render service before
   the first real trade ever goes in.
