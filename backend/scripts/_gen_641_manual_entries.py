"""Throwaway: generate the manual_entry.py JSON payload reconstructing account
641's full single-stock trading history (2013/2014 original lots through the
Aug 2025 close-out/transfer into Rule 1), per user sign-off:

- ETFs and other fractional-only positions are out of scope (skipped).
- DRIP-only stocks (AWK, BAH, INTC, QCOM, SIRI, MMM, DIS) get their original
  lot rounded to the nearest whole share, plus one consolidated "DRIP
  accumulation" BTO trued up to the real final whole-share count, costed at
  the real total dividends reinvested over the years.
- In-window stock splits that only change share count with no intervening
  sale (GOOG/GOOGL 20:1) restate the original lot: same acquisition date,
  split-adjusted quantity and per-share cost, unchanged total basis.
  Splits with an intervening sale (NVDA) and TSLA's 3:1 still use a
  zero-cost catch-up BTO for the extra shares.
- Positions that were transferred into Rule 1 (AWK, GOOG, GOOGL, BRK.B, NVDA,
  TSLA) simply continue -- no disposal event needed.
- Positions fully liquidated before the transfer (BA, BAH, INTC, QCOM, SIRI,
  MMM, DIS, UAA, UA, SPCE, SOLV) get a final STC sized to their real proceeds.
- Options activity (NVDA/TSLA short calls, all opened+closed/expired within
  641, never assigned) is handled separately -- not in this file.
"""

import json

BATCH = "acct641_migration"
ACCOUNT = "Rule 1"

rows: list[dict] = []


def bto(ticker, dt, shares, price, notes):
    rows.append(
        {
            "kind": "bto",
            "account": ACCOUNT,
            "ticker": ticker,
            "date": dt,
            "shares": shares,
            "price": f"{price:.4f}",
            "commission_per_share": "0",
            "notes": notes,
            "import_batch": BATCH,
        }
    )


def stc(ticker, dt, shares, price, notes):
    rows.append(
        {
            "kind": "stc",
            "account": ACCOUNT,
            "ticker": ticker,
            "date": dt,
            "shares": shares,
            "price": f"{price:.4f}",
            "commission_per_share": "0",
            "notes": notes,
            "import_batch": BATCH,
        }
    )


def dividend(ticker, dt, amount, notes):
    rows.append(
        {
            "kind": "dividend",
            "account": ACCOUNT,
            "ticker": ticker,
            "date": dt,
            "amount": f"{amount:.2f}",
            "description": notes,
            "import_batch": BATCH,
        }
    )


ORIGIN_NOTE = (
    "Acct 641->Rule 1 reconstruction: original lot per 2021-05-31 statement "
    "(pre-dates our records; Schwab-reported cost basis used as-is)."
)

# ---- Transferred positions (continue in Rule 1, no disposal) ----
bto("TSLA", "2013-11-06", 200, 44.9810, ORIGIN_NOTE)
bto("TSLA", "2022-08-25", 400, 0.0, "Acct 641: TSLA 3:1 stock split (Aug 2022). 200 -> 600 shares, no cost-basis change (zero-cost synthetic lot).")

bto("GOOG", "2014-02-25", 100, 30.2851, ORIGIN_NOTE + " Jul 2022 20:1 split restated on the original lot (5 sh @ 605.7020 -> 100 sh @ 30.2851; total basis unchanged).")
bto("GOOGL", "2014-02-25", 100, 30.4619, ORIGIN_NOTE + " Jul 2022 20:1 split restated on the original lot (5 sh @ 609.2380 -> 100 sh @ 30.4619; total basis unchanged).")

bto("BRK.B", "2015-08-28", 14, 135.5300, ORIGIN_NOTE + " Delivered out 12/2022, journaled back in 6/2023 during TDA->Schwab platform migration; same 14 shares, cost basis preserved -- not modeled as separate trades.")

# NVDA: real BTO within our window, then 4:1 split, a real sale to round off
# before the 2nd split, then 10:1 split. Small DRIP fractions around this are
# skipped per the fractional-share decision (immaterial, final count already
# nets to a clean 200 without them).
bto("NVDA", "2021-05-28", 7, 620.5400, "Acct 641->Rule 1 reconstruction: original BTO (within our statement window).")
bto("NVDA", "2021-07-20", 21, 0.0, "Acct 641: NVDA 4:1 stock split (Jul 2021). 7 -> 28 shares, no cost-basis change (zero-cost synthetic lot).")
stc("NVDA", "2024-06-03", 8, 1107.3250, "Acct 641: sale ahead of NVDA 10:1 split to round position to an even 20 shares before the split (real proceeds $8,858.60 for 8.0535 fractional shares; rounded to 8 whole shares here, price backed into real proceeds).")
bto("NVDA", "2024-06-10", 180, 0.0, "Acct 641: NVDA 10:1 stock split (Jun 2024). 20 -> 200 shares, no cost-basis change (zero-cost synthetic lot).")

bto("AWK", "2014-02-25", 68, 5070.67 / 68, ORIGIN_NOTE + " (67.576 actual shares rounded to 68.)")
bto("AWK", "2025-08-01", 4, 657.24 / 4, "Acct 641: consolidated 2021-2025 dividend-reinvestment (DRIP) accumulation, trued up to the 72 whole shares actually transferred into Rule 1 in Aug 2025. Real total DRIP reinvested ~$657.24 across ~4.56 fractional shares over 4 years; rounded to 4 whole shares here for schema compatibility (share counts are integer-only).")

# ---- Fully liquidated positions (no longer in Rule 1's real 641 transfer) ----
bto("BA", "2020-04-08", 20, 2957.95 / 20, ORIGIN_NOTE)
stc("BA", "2024-10-15", 20, 2999.92 / 20, "Acct 641: final full liquidation of BA position before eventual transfer of remaining tickers to Rule 1.")

bto("BAH", "2020-04-17", 10, 782.97 / 10, ORIGIN_NOTE + " (10.156 actual shares rounded to 10.)")
bto("BAH", "2025-04-01", 1, 70.85, "Acct 641: consolidated 2021-2025 DRIP accumulation, trued up to ~11 whole shares held just before final sale. Real total DRIP reinvested ~$70.85; rounded to 1 whole share here.")
stc("BAH", "2025-04-04", 11, 1199.04 / 11, "Acct 641: final full liquidation of BAH position.")

bto("INTC", "2016-01-27", 29, 1195.83 / 29, ORIGIN_NOTE + " (28.682 actual shares rounded to 29.)")
bto("INTC", "2024-06-15", 2, 99.95, "Acct 641: consolidated 2021-2024 DRIP accumulation, trued up to ~31 whole shares held just before final sale. Real total DRIP reinvested ~$99.95; rounded to 2 whole shares here.")
stc("INTC", "2024-07-01", 31, 962.92 / 31, "Acct 641: final full liquidation of INTC position.")

bto("QCOM", "2014-02-25", 28, 2008.75 / 28, ORIGIN_NOTE + " (27.535 actual shares rounded to 28.)")
bto("QCOM", "2025-07-01", 1, 328.65, "Acct 641: consolidated 2021-2025 DRIP accumulation, trued up to 29 whole shares held just before final sale. Real total DRIP reinvested ~$328.65; rounded to 1 whole share here.")
stc("QCOM", "2025-07-31", 29, 4650.15 / 29, "Acct 641: final full liquidation of QCOM position.")

bto("SIRI", "2018-01-25", 1, 5.80, ORIGIN_NOTE + " (1.008 actual shares rounded to 1; DRIP growth over the years was negligible, no catch-up lot needed.)")
stc("SIRI", "2024-06-03", 1, 3.17, "Acct 641: final full liquidation of tiny SIRI DRIP position.")

bto("MMM", "2015-08-27", 10, 1479.71 / 10, ORIGIN_NOTE + " (10.351 actual shares rounded to 10.)")
bto("MMM", "2024-05-01", 2, 178.59, "Acct 641: consolidated 2021-2024 DRIP accumulation, trued up to ~12 whole shares held just before final sale (which also triggered the Solventum spinoff). Real total DRIP reinvested ~$178.59; rounded to 2 whole shares here.")
stc("MMM", "2024-06-03", 12, 1175.16 / 12, "Acct 641: final full liquidation of MMM position (same day as the Solventum spinoff shares were also sold).")

bto("DIS", "2014-02-07", 21, 1762.43 / 21, ORIGIN_NOTE + " (20.872 actual shares rounded to 21; DRIP growth through the final sale date rounds to the same 21 shares, no catch-up lot needed.)")
stc("DIS", "2025-04-07", 21, 1821.13 / 21, "Acct 641: final full liquidation of DIS position.")

bto("UAA", "2014-02-14", 41, 1484.22 / 41, ORIGIN_NOTE)
stc("UAA", "2023-10-19", 41, 286.99 / 41, "Acct 641: final full liquidation of UAA (Under Armour Class A) position.")

bto("UA", "2014-02-14", 18, 470.08 / 18, ORIGIN_NOTE)
stc("UA", "2023-10-19", 18, 117.00 / 18, "Acct 641: final full liquidation of UA (Under Armour Class C) position.")

bto("SPCE", "2020-04-15", 100, 1811.00 / 100, ORIGIN_NOTE)
stc("SPCE", "2023-10-23", 100, 172.99 / 100, "Acct 641: final full liquidation of SPCE (Virgin Galactic) position.")

# SOLV: 3M (MMM) spinoff, immediately liquidated. Simplified to zero-cost
# BTO (spinoff shares received "free" from the parent) then a single STC for
# the combined whole-share sale + cash-in-lieu proceeds. Not tax-precise
# (real spinoffs reallocate a slice of the parent's basis) but captures the
# real ~$178 cash impact; immaterial given the small dollar amount.
bto("SOLV", "2024-04-01", 2, 0.0, "Acct 641: Solventum Corp (SOLV) spinoff from 3M (MMM), Apr 2024. Simplified as a zero-cost lot rather than reallocating a slice of MMM's basis (immaterial, ~$178 total).")
stc("SOLV", "2024-06-03", 2, (61.30 + 116.60) / 2, "Acct 641: final liquidation of SOLV spinoff shares -- combines the $61.30 cash-in-lieu (fractional shares) and $116.60 sale (2 whole shares) into one STC for simplicity.")

# ---- Dividends actually received in cash (not reinvested) while held in 641 ----
# (DRIP-reinvested dividends are already folded into the consolidated
# catch-up BTOs above and are NOT double-counted here as cash_flows.) DRIP
# appears to have been switched off for these tickers starting ~2025 --
# these show up as "Dividend/Qual.Dividend" (cash) rather than
# "Dividend/QualDivReinvest" with no accompanying Purchase/ReinvestedShares.
CASH_DIV_NOTE = "Acct 641: dividend paid in cash (DRIP appears switched off from this point), continues to be Rule 1's dividend after the transfer."
dividend("AWK", "2025-03-04", 55.19, CASH_DIV_NOTE)
dividend("AWK", "2025-06-03", 59.69, CASH_DIV_NOTE)
dividend("GOOGL", "2025-03-17", 20.07, CASH_DIV_NOTE)
dividend("GOOGL", "2025-06-16", 21.07, CASH_DIV_NOTE)
dividend("GOOG", "2025-03-17", 20.07, CASH_DIV_NOTE)
dividend("GOOG", "2025-06-16", 21.07, CASH_DIV_NOTE)
dividend("NVDA", "2025-04-02", 2.00, CASH_DIV_NOTE)
dividend("NVDA", "2025-07-03", 2.00, CASH_DIV_NOTE)

print(json.dumps(rows, indent=2))
print(f"\n# total rows: {len(rows)}", file=__import__("sys").stderr)
