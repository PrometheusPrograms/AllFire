"""Generate manual_entry JSON for lots reconstructed from Schwab/TDA PDFs
that the Trader API window never saw (or under-counted).

Covers the three remaining validate_import negative-share flags as of the
2013–2026 statement dump:

- Roth TSLA: original 30 shares (2013-11-07) plus 5:1 and 3:1 splits.
- Roth CWT: original 30 shares (2014-02-25) plus one whole-share DRIP
  catch-up (fractional DRIP stays cash-only).
- Rule 1 OXY: the 2026-04-20 buy is 500 shares on the 742 statement;
  Schwab API only booked 64. Catch-up 436 at the same price.

ETFs and account 730 are out of scope. STC rows already in the ledger
(Roth TSLA 50 on 2024-12-24, Roth CWT 35 on 2026-05-13) are not repeated.
"""

import json
from pathlib import Path

rows: list[dict] = []


def bto(account, ticker, dt, shares, price, notes, batch):
    rows.append(
        {
            "kind": "bto",
            "account": account,
            "ticker": ticker,
            "date": dt,
            "shares": shares,
            "price": f"{price:.4f}",
            "commission_per_share": "0",
            "notes": notes,
            "import_batch": batch,
        }
    )


ROTH = "Roth"
RULE1 = "Rule 1"
BATCH_467 = "acct467_roth_history"
BATCH_OXY = "oxy_stmt_catchup"

bto(
    ROTH,
    "TSLA",
    "2013-11-07",
    30,
    140.2567,
    "Acct 467 Roth reconstruction: original lot per 2014-01-31 TDA statement "
    "(30 sh purchased 2013-11-07, Schwab-reported cost $4,207.70). "
    "Pre-dates the Schwab API window.",
    BATCH_467,
)
bto(
    ROTH,
    "TSLA",
    "2020-08-31",
    120,
    0.0,
    "Acct 467: TSLA 5:1 stock split (Aug 2020). 30 -> 150 shares, no cost-basis "
    "change (zero-cost synthetic lot). Confirmed on 2020-08-31 TDA statement.",
    BATCH_467,
)
bto(
    ROTH,
    "TSLA",
    "2022-08-25",
    300,
    0.0,
    "Acct 467: TSLA 3:1 stock split (Aug 2022). 150 -> 450 shares, no cost-basis "
    "change (zero-cost synthetic lot). Confirmed on 2022-08-31 TDA statement. "
    "2024-12-26 sale of 50 sh is already a Schwab STC.",
    BATCH_467,
)
bto(
    ROTH,
    "CWT",
    "2014-02-25",
    30,
    23.9467,
    "Acct 467 Roth reconstruction: original lot per 2014-02-28 TDA statement "
    "(30 sh purchased 2014-02-25, cost $718.40 including commission). "
    "Pre-dates the Schwab API window. DRIP remains cash_flows / 0-share BTO; "
    "this row is the rounded original lot.",
    BATCH_467,
)
bto(
    ROTH,
    "CWT",
    "2024-11-22",
    5,
    49.6900,
    "Acct 467: CWT DRIP catch-up to whole shares. Statement qty grew from 30 "
    "to 35.434 (cost $977.24) then a fractional sale left 35 sh @ $966.85 "
    "(2026-04-30). Five whole shares at $248.45 ($966.85 - $718.40) dated on "
    "the last in-window DRIP. 2026-05-14 sale of 35 sh is already a Schwab STC.",
    BATCH_467,
)
bto(
    RULE1,
    "OXY",
    "2026-04-20",
    436,
    53.95,
    "Rule 1 (742) 2026-04-30 statement: Purchase 500 OXY @ 53.95 on 2026-04-20. "
    "Schwab API only booked 64 sh at that price; this is the remaining 436 so "
    "EOM inventory matches 638 before the 2026-05-15 600-share call assignment.",
    BATCH_OXY,
)

out = Path(__file__).with_name("data") / "statement_gap_lots.json"
out.write_text(json.dumps(rows, indent=2) + "\n")
print(f"wrote {len(rows)} rows to {out}")
