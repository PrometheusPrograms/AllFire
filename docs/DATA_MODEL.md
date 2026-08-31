# Data Model (v2 — Postgres)

**Status:** Sample data only — this is the schema to build fresh in Postgres/Neon before
any real trades go in. No migration needed; just seed test data against this directly.

This revises the working SQLite schema (`fire_db.html`) with three changes agreed on:

1. Trade lifecycle becomes append-only (`trade_events`), so trade history is never
   overwritten — it's always reconstructable.
2. `cost_basis`'s running totals are computed at query time, not stored, so a corrected or
   backfilled row can never leave stale totals downstream of it.
3. Every money/price/ratio column is `NUMERIC` with real Postgres fixed-point precision —
   SQLite's `NUMERIC` doesn't actually guarantee this the way Postgres's does, so this was a
   silent risk in the old schema, not just a style issue.

All foreign keys below are real, enforced constraints — this is on by default in Postgres,
unlike SQLite where it has to be turned on per connection and is easy to forget.

## 1. Reference tables

```sql
CREATE TABLE accounts (
    id                SERIAL PRIMARY KEY,
    account_name      TEXT NOT NULL,
    account_type      TEXT NOT NULL,             -- 'RETIREMENT' | 'INVESTMENT' | ...
    start_date        DATE NOT NULL,
    starting_balance   NUMERIC(14,2) NOT NULL DEFAULT 0,
    is_default          BOOLEAN NOT NULL DEFAULT false,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tickers (
    id             SERIAL PRIMARY KEY,
    ticker         TEXT NOT NULL UNIQUE,
    company_name    TEXT,
    sector          TEXT,
    market_cap       NUMERIC(18,2),
    needs_update      BOOLEAN NOT NULL DEFAULT false,  -- flips true when stale; your future refresh agent picks these up
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Strategy registry — describes what fields each trade type needs, so adding a
-- new strategy is a data insert, not a schema change. (This pattern was already
-- right in the old schema — kept as-is.)
CREATE TABLE trade_types (
    id                     SERIAL PRIMARY KEY,
    type_name              TEXT NOT NULL UNIQUE,      -- 'ROCT PUT', 'BTO', 'STC', ...
    description             TEXT,
    category                 TEXT NOT NULL,              -- 'OPTIONS' | 'STOCK'
    is_credit                 BOOLEAN NOT NULL DEFAULT false,
    requires_expiration       BOOLEAN NOT NULL DEFAULT false,
    requires_strike           BOOLEAN NOT NULL DEFAULT false,
    requires_contracts        BOOLEAN NOT NULL DEFAULT false,
    requires_shares           BOOLEAN NOT NULL DEFAULT false,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seeded (see alembic/versions/32c3020a1e5a_seed_trade_types.py) with the 7 types
-- actually in use: ROCT PUT, RULE ONE PUT, ROCT CALL, RULE ONE CALL (all OPTIONS),
-- ROCS BULL PUT SPREAD (OPTIONS, uses trades.long_strike), and BTO/STC (STOCK,
-- plain buy/sell that feeds cost_basis directly, no options involved).
-- `ROCT *` = trading shares (short-term). `RULE ONE *` = long-term investment shares
-- (`RULE ONE CALL` sells long-term shares at/above full intrinsic value). This
-- distinction lives in each row's `description` today; promote it to a structured
-- column if/when the trading-vs-long-term portfolio view (see ARCHITECTURE.md
-- roadmap) needs to query on it directly instead of parsing description text.
CREATE TABLE commissions (
    id                 SERIAL PRIMARY KEY,
    account_id          INTEGER NOT NULL REFERENCES accounts(id),
    commission_rate      NUMERIC(10,6) NOT NULL,   -- per-share rate; NUMERIC not REAL, this is money math
    effective_date        DATE NOT NULL,
    notes                   TEXT,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## 2. Trades — immutable setup, split from mutable lifecycle

```sql
-- What you decided to do. Written once at entry. Never updated after that —
-- everything that happens afterward (close, roll, expiration) is a trade_events row.
CREATE TABLE trades (
    id                      SERIAL PRIMARY KEY,
    account_id               INTEGER NOT NULL REFERENCES accounts(id),
    ticker_id                 INTEGER NOT NULL REFERENCES tickers(id),
    ticker                     TEXT NOT NULL,   -- TODO: confirm whether this should stay as an
                                                  -- intentional snapshot (protects history if a
                                                  -- ticker symbol is ever renamed) or be dropped
                                                  -- in favor of always joining tickers.ticker
    trade_type_id               INTEGER NOT NULL REFERENCES trade_types(id),
    trade_type                   TEXT NOT NULL,   -- same TODO as above, for the type_name snapshot
    trade_parent_id                INTEGER REFERENCES trades(id),   -- self-reference for rolls
    date_trade_open                 DATE NOT NULL,
    expiration_date                   DATE,
    days_to_expiration                 INTEGER,
    num_of_contracts                    INTEGER,
    num_of_shares                        INTEGER,
    strike_price                          NUMERIC(12,4),
    long_strike                            NUMERIC(12,4),
    price_per_share                          NUMERIC(12,4),
    current_price                             NUMERIC(12,4),
    credit_debit                               NUMERIC(12,4) NOT NULL,
    total_premium                               NUMERIC(14,2),
    commission_per_share                          NUMERIC(10,6) NOT NULL,
    total_amount                                   NUMERIC(14,2),
    margin_capital                                  NUMERIC(14,2),
    margin_percent                                   NUMERIC(8,6),
    net_credit_per_share                               NUMERIC(12,6),
    risk_capital_per_share                              NUMERIC(12,4),
    arorc                                                NUMERIC(10,6),
    schwab_order_id                                        TEXT,
    schwab_activity_id                                     TEXT UNIQUE,  -- Schwab CLI; stable when order id is missing
    notes                                                   TEXT,
    needs_review                                             BOOLEAN NOT NULL DEFAULT false,
    import_batch                                               TEXT,  -- bookkeeping only, see PRODUCTION_IMPORT_RUNBOOK.md §4; null for hand-entered trades
    created_at                                                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_trades_account ON trades(account_id);
CREATE INDEX idx_trades_ticker ON trades(ticker_id);
CREATE INDEX idx_trades_parent ON trades(trade_parent_id);
CREATE INDEX idx_trades_import_batch ON trades(import_batch);

-- Append-only lifecycle log. This is the entire answer to "immutable history":
-- a trade's status, close date, and closing debit are never columns on `trades`
-- you overwrite — they're rows here that accumulate, in order, forever.
CREATE TABLE trade_events (
    id                SERIAL PRIMARY KEY,
    trade_id           INTEGER NOT NULL REFERENCES trades(id),
    event_type          TEXT NOT NULL CHECK (event_type IN ('OPEN','ROLL','ADJUST','CLOSE','EXPIRE','ASSIGN')),
    event_date            DATE NOT NULL,
    closing_debit          NUMERIC(12,4),
    total_debit              NUMERIC(14,2),
    schwab_order_id            TEXT,           -- the closing/rolling order, distinct from the opening one on `trades`
    notes                       TEXT,
    import_batch                 TEXT,           -- bookkeeping only, see PRODUCTION_IMPORT_RUNBOOK.md §4; null for hand-entered events
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now()
    -- Deliberately no updated_at, no soft-delete flag: rows are never modified
    -- after insert. If a mistake needs correcting, insert a new ADJUST row that
    -- says so in notes — don't edit the old one.
);

CREATE INDEX idx_trade_events_trade ON trade_events(trade_id);
CREATE INDEX idx_trade_events_import_batch ON trade_events(import_batch);

-- Current status, derived — never stored, so it can never drift from the truth.
CREATE VIEW v_trade_current_status AS
SELECT
    t.id AS trade_id,
    t.account_id,
    t.ticker_id,
    CASE
        WHEN EXISTS (SELECT 1 FROM trade_events e WHERE e.trade_id = t.id AND e.event_type IN ('CLOSE','EXPIRE','ASSIGN'))
        THEN 'closed' ELSE 'open'
    END AS trade_status,
    (SELECT event_date FROM trade_events e WHERE e.trade_id = t.id AND e.event_type IN ('CLOSE','EXPIRE','ASSIGN') ORDER BY event_date DESC LIMIT 1) AS date_trade_closed,
    (SELECT closing_debit FROM trade_events e WHERE e.trade_id = t.id AND e.event_type IN ('CLOSE','EXPIRE','ASSIGN') ORDER BY event_date DESC LIMIT 1) AS closing_debit,
    (SELECT event_date FROM trade_events e WHERE e.trade_id = t.id AND e.event_type = 'ROLL' ORDER BY event_date DESC LIMIT 1) AS date_trade_rolled
FROM trades t;
```

## 3. Cash flows, cost basis, bankroll

```sql
CREATE TABLE cash_flows (
    id                 SERIAL PRIMARY KEY,
    account_id          INTEGER NOT NULL REFERENCES accounts(id),
    ticker_id             INTEGER REFERENCES tickers(id),
    trade_id               INTEGER REFERENCES trades(id),
    transaction_date         DATE NOT NULL,
    transaction_type           TEXT NOT NULL,      -- 'SELL PUT', 'BUY TO CLOSE', 'DIVIDEND', ...
    amount                       NUMERIC(12,2) NOT NULL,
    description                    TEXT,
    schwab_activity_id               TEXT UNIQUE,  -- Schwab CLI import; null otherwise
    import_batch                       TEXT,         -- bookkeeping; see SCHWAB_IMPORT.md
    created_at                       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_cash_flows_trade ON cash_flows(trade_id);

-- Append-only ledger of basis-affecting transactions. Already close to right in the
-- old schema — the only change is dropping the stored running_basis/running_shares
-- columns in favor of computing them, below.
CREATE TABLE cost_basis (
    id                  SERIAL PRIMARY KEY,
    account_id            INTEGER NOT NULL REFERENCES accounts(id),
    ticker_id               INTEGER NOT NULL REFERENCES tickers(id),
    trade_id                  INTEGER REFERENCES trades(id),
    cash_flow_id                INTEGER REFERENCES cash_flows(id),
    transaction_date              DATE NOT NULL,
    description                     TEXT,
    shares                            INTEGER NOT NULL,
    cost_per_share                     NUMERIC(12,4) NOT NULL,
    total_amount                         NUMERIC(14,2) NOT NULL,
    created_at                             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_cost_basis_ticker_account ON cost_basis(account_id, ticker_id, transaction_date);

-- Running basis/shares computed at read time — a corrected or backfilled row here
-- can never leave stale totals downstream, because there ARE no stored totals.
CREATE VIEW v_cost_basis_running AS
SELECT
    cb.*,
    SUM(cb.total_amount) OVER (
        PARTITION BY cb.account_id, cb.ticker_id ORDER BY cb.transaction_date, cb.id
    ) AS running_basis,
    SUM(cb.shares) OVER (
        PARTITION BY cb.account_id, cb.ticker_id ORDER BY cb.transaction_date, cb.id
    ) AS running_shares
FROM cost_basis cb;
-- basis_per_share = running_basis / NULLIF(running_shares, 0), computed in the app
-- layer or as an added column here — guard the division by zero either way.

CREATE TABLE bankroll (
    id                   SERIAL PRIMARY KEY,
    account_id             INTEGER NOT NULL REFERENCES accounts(id),
    transaction_date         DATE NOT NULL,
    transaction_amount         NUMERIC(14,2) NOT NULL,   -- signed: deposit positive, withdrawal negative
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## 4. Open item

`trades.ticker` and `trades.trade_type` duplicate `ticker_id`/`trade_type_id` as text. Left
in place above with a `TODO` — worth deciding once you've investigated whether the old
schema was doing this on purpose (a snapshot, so a trade still shows "CELH" even if that
ticker were ever renamed in the `tickers` table) or by accident. If it's intentional, keep
it and just document it as such in the column comment; if not, drop the text columns and
always join through the `_id` foreign key instead — one less place for the two to drift
apart.
