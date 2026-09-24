"""Throwaway batch-validation harness for the legacy TDA parser: parse every
TDA-format statement and check extracted transaction totals reconcile
against the "Cash Activity Summary" (Securities Purchased/Sold, Income)
figures on page ~3 of each statement. Not part of the shipped CLI surface.
"""

import re
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import pdfplumber

from scripts.schwab_legacy_statement_parser import parse_legacy_statement
from scripts.schwab_statement_parser import _money

STATEMENTS_DIR = Path("/Users/yuriy/Documents/Schwab statements")


def _extract_cash_activity_summary(pdf: pdfplumber.PDF) -> dict[str, Decimal] | None:
    for page in pdf.pages:
        text = page.extract_text() or ""
        if "Cash Activity Summary" not in text:
            continue
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        lines: dict[float, list[dict]] = defaultdict(list)
        for w in words:
            lines[round(w["top"], 0)].append(w)
        result: dict[str, Decimal] = {}
        for top, line_words in lines.items():
            line_words = sorted(line_words, key=lambda w: w["x0"])
            texts = [w["text"] for w in line_words]
            joined = " ".join(texts)
            for label, key in [
                ("Securities Purchased", "purchases"),
                ("Securities Sold", "sales"),
                ("Income", "income"),
                ("Expense", "expense"),
            ]:
                if joined.startswith(label):
                    rest_words = [w for w in line_words if w["x0"] < 270 and w["text"] not in label.split()]
                    tokens = [w["text"] for w in rest_words if re.match(r"^\(?-?\$?[\d,]+\.\d{2}\)?$|^-$", w["text"])]
                    if tokens:
                        val = _money(tokens[0])
                        if val is not None:
                            result[key] = val
                    break
        if result:
            return result
    return None


def main() -> None:
    files = sorted(STATEMENTS_DIR.glob("TDA*.PDF"))
    print(f"legacy files: {len(files)}")

    ok = 0
    mismatches = []
    errors = []
    for f in files:
        try:
            result = parse_legacy_statement(f)
        except Exception as exc:  # noqa: BLE001
            errors.append((f.name, str(exc)))
            continue
        with pdfplumber.open(f) as pdf:
            summary = _extract_cash_activity_summary(pdf)
        if summary is None:
            errors.append((f.name, "no cash activity summary found"))
            continue

        by_activity: dict[str, Decimal] = defaultdict(Decimal)
        for t in result.transactions:
            if t.amount is None or t.activity is None:
                continue
            if t.activity.startswith("Journal - Other"):
                continue
            by_activity[t.activity] += t.amount

        computed_purchases = -by_activity.get("Buy - Securities Purchased", Decimal(0))
        computed_sales = by_activity.get("Sell - Securities Sold", Decimal(0))
        computed_income = sum(
            (v for k, v in by_activity.items() if k.startswith("Div/Int") and "Income" in k),
            Decimal(0),
        )

        diffs = []
        if "purchases" in summary and abs(computed_purchases - summary["purchases"].copy_abs()) > Decimal("0.02"):
            diffs.append(f"purchases {computed_purchases} vs {summary['purchases']}")
        if "sales" in summary and abs(computed_sales - summary["sales"]) > Decimal("0.02"):
            diffs.append(f"sales {computed_sales} vs {summary['sales']}")
        if "income" in summary and abs(computed_income - summary["income"]) > Decimal("0.02"):
            diffs.append(f"income {computed_income} vs {summary['income']}")

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
