# Schwab CLI import (dividends + BTO + STC)

Gap-fills what OKW never wrote: **cash dividends** into `cash_flows`, and
**outright equity buys/sells** into `BTO`/`STC` trades plus `cost_basis`.
Option trades already imported from the spreadsheet are not synced from
Schwab — an equity leg that Schwab describes as an assignment/exercise is
left for the OKW/`trade_events` ASSIGN flow, not written here.

This is a CLI against a `DATABASE_URL` you supply — not an app route, not
OAuth in the browser UI.

## 1. Schwab developer app (once)

1. Create an app at [developer.schwab.com](https://developer.schwab.com).
2. Set the callback URL to match `SCHWAB_REDIRECT_URI` (default
   `https://127.0.0.1:8182`).
3. Put the app key/secret in `backend/.env` (never commit them):

```
SCHWAB_CLIENT_ID=...
SCHWAB_CLIENT_SECRET=...
SCHWAB_REDIRECT_URI=https://127.0.0.1:8182
SCHWAB_ACCOUNT_HASH_RULE1=   # optional; from GET /accounts/accountNumbers
SCHWAB_ACCOUNT_HASH_ROTH=
```

`--account rule1|roth` maps to the same DB names as historical import (`Rule 1`,
`Roth`).

## 2. Login (stores a gitignored token file)

From `backend/`:

```
python -m scripts.schwab_auth
```

Open the printed URL, sign in, then paste the **full** redirected URL (it
contains `?code=`). Tokens land in `backend/.schwab_tokens.json`. Refresh
tokens expire on Schwab's schedule (historically about a week) — re-run
`schwab_auth` if a refresh fails.

## 3. Import

Imports write only to the `DATABASE_URL` you pass — they do not sync Neon branches.
Experiments belong on **dev**. The ledger you will live with is imported on **staging**,
then **production**, with the same date range. See [ENVIRONMENTS.md](ENVIRONMENTS.md).

Always `--dry-run` first, against **dev**, not production:

```
python -m scripts.import_schwab \
  --database-url "$DATABASE_URL" \
  --account rule1 \
  --start 2024-01-01 \
  --end 2026-12-31 \
  --dry-run
```

Then drop `--dry-run`. Repeat with `--account roth`. Pass
`--schwab-account-hash` if the env vars are unset.

Idempotency is **per Schwab activity id**, not wipe-and-replace. Re-running
the same date range skips rows already stored. `import_batch` is
`schwab_rule1_div_bto` / `schwab_roth_div_bto` (bookkeeping only — the name
predates STC support but is kept for `import_batch` continuity).

## 4. What is written vs skipped

| Schwab row | Action |
|---|---|
| Cash dividend (`DIVIDEND` / `DIVIDEND_OR_INTEREST`) | `cash_flows` (`transaction_type='DIVIDEND'`) |
| Equity BUY | `trades` (`BTO`) + `OPEN` event + `cost_basis` |
| Equity SELL (plain, not assignment/exercise) | `trades` (`STC`) + `OPEN` event + `cost_basis` (negative shares/amount — proceeds reduce basis) |
| Equity SELL (plain, not assignment/exercise) | `trades` (`STC`) + `OPEN` event + `cost_basis` (negative shares/amount — proceeds reduce basis) |
| Same activity/order id already in DB | Skip |
| Equity BUY matching an existing BTO (account, ticker, date, shares, similar price) | Skip |
| Equity SELL matching an existing STC (same match keys) | Skip |
| Equity BUY matching an ASSIGN cost-basis lot (or contracts×100 at/near the strike within 45 settlement days) | Skip (OKW assignment, not a BTO) |
| Equity SELL matching a CALL ASSIGN lot (contracts×100 at/near the strike within 45 settlement days; a fill may cover several lots) | Skip (OKW assignment, not an STC) |
| DRIP, interest, option fills, journals, assignment/exercise, incomplete rows (missing ticker/shares/price) | Printed as `review` — not imported. Work these via `scripts/manual_entry.py` or an explicit skip (see PRODUCTION_IMPORT_RUNBOOK.md §3). |

Both BTO and STC math roll up automatically: `cost_basis` running totals and
`v_cost_basis_running` are computed at query time from whatever rows exist,
so no separate "recompute" step is needed after an import — the app's
existing trades/analytics endpoints reflect new rows on the next request.
Share quantities are rounded to the nearest whole share (`ROUND_HALF_UP`);
truncating with `int()` under-counted lots (e.g. LULU 9 vs statement 10).

## 5. Schema keys

- `cash_flows.schwab_activity_id` (unique), `cash_flows.import_batch`
- `trades.schwab_activity_id` (unique) — used even when Schwab omits `orderId`
- `trades.schwab_order_id` — Schwab order id when present, else the activity id
