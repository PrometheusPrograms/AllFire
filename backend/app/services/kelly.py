"""Kelly Criterion position sizing.

Formula verified against the "KELLY FORMULA" section of `OKW_2026.xlsx`'s
`Templates` sheet (cell formulas):

    AVG LOSS (l) = RiskCapital * PctRiskCapitalAtRisk
    R = w/l       = NetCredit / AvgLoss            (win/loss ratio, "odds")
    Kelly         = W - ((1 - W) / R)               (W = probability of winning)

`avg_win` / `avg_loss` are passed in directly rather than recomputed here,
since how they're derived (e.g. `avg_loss = risk_capital_per_share *
pct_risk_capital_at_risk` in the sheet) is a modeling choice made by the
caller — this keeps the Kelly formula itself a small, independently
testable function.

Mirrors the spreadsheet's own error handling: `R = IFERROR(NC/l, 0)` then
`Kelly = IFERROR(W-(1-W)/R, 0)` — an undefined win/loss ratio (zero or
negative avg_loss) falls back to a Kelly fraction of zero rather than
raising.
"""

from decimal import Decimal

ZERO = Decimal("0")
ONE = Decimal("1")


def calculate_kelly(
    probability_of_winning: Decimal,
    avg_win: Decimal,
    avg_loss: Decimal,
) -> Decimal:
    """Kelly = W - (1 - W) / R, where R = avg_win / avg_loss.

    Returns `Decimal("0")` if `avg_loss` is not positive (undefined
    win/loss ratio), matching the spreadsheet's `IFERROR(..., 0)` chain.
    """
    if avg_loss <= ZERO:
        return ZERO
    win_loss_ratio = avg_win / avg_loss
    if win_loss_ratio == ZERO:
        return ZERO
    return probability_of_winning - (ONE - probability_of_winning) / win_loss_ratio
