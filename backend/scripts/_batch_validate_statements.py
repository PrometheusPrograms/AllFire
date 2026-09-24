"""Throwaway batch-validation harness: parse every statement PDF and check
that extracted transaction totals reconcile against each statement's own
"Transactions - Summary" line. Not part of the shipped CLI surface.
"""

import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from scripts.schwab_statement_parser import parse_statement

STATEMENTS_DIR = Path("/Users/yuriy/Documents/Schwab statements")


def main() -> None:
    files = sorted(STATEMENTS_DIR.glob("*.PDF"))
    legacy = [f for f in files if f.name.startswith("TDA")]
    current = [f for f in files if not f.name.startswith("TDA")]
    print(f"total files: {len(files)}  legacy(TDA): {len(legacy)}  current: {len(current)}")

    ok = 0
    mismatches = []
    errors = []
    for f in current:
        try:
            result = parse_statement(f)
        except Exception as exc:  # noqa: BLE001
            errors.append((f.name, str(exc)))
            continue
        if result.summary is None:
            errors.append((f.name, "no summary found"))
            continue
        by_cat: dict[str, Decimal] = defaultdict(Decimal)
        for t in result.transactions:
            if t.amount is not None:
                by_cat[t.category or "?"] += t.amount
        computed_purchases = by_cat.get("Purchase", Decimal(0))
        computed_sales = by_cat.get("Sale", Decimal(0)) + by_cat.get("Redemption", Decimal(0))
        computed_div_int = by_cat.get("Interest", Decimal(0)) + by_cat.get("Dividend", Decimal(0))
        s = result.summary
        diffs = []
        if s.purchases is not None and abs(computed_purchases - s.purchases) > Decimal("0.02"):
            diffs.append(f"purchases {computed_purchases} vs {s.purchases}")
        if s.sales is not None and abs(computed_sales - s.sales) > Decimal("0.02"):
            diffs.append(f"sales {computed_sales} vs {s.sales}")
        if s.dividends_interest is not None and abs(computed_div_int - s.dividends_interest) > Decimal("0.02"):
            diffs.append(f"div/int {computed_div_int} vs {s.dividends_interest}")
        if diffs:
            mismatches.append((f.name, diffs))
        else:
            ok += 1

    print(f"OK: {ok}  mismatches: {len(mismatches)}  errors: {len(errors)}")
    for name, diffs in mismatches:
        print(f"MISMATCH {name}: {diffs}")
    for name, err in errors:
        print(f"ERROR {name}: {err}")


if __name__ == "__main__":
    sys.exit(main())
