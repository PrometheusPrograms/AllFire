"""Return on Risk Capital (RORC) and its annualized form (ARORC).

Formulas verified against `OKW_2026.xlsx`'s "ARORC Calculator" and
"Templates" sheets (cell formulas, not just displayed values):

    RiskCapital (RC) = Strike - NetCredit
    RORC          = NetCredit / RC
    Multiplier    = 365 / DaysToExpiration
    ARORC         = RORC * Multiplier

`margin_percent` extends the plain spreadsheet formula for trades held on
margin rather than fully cash-secured (less than 100% of risk capital
actually committed) — this is the production formula already reconciled
against the spreadsheet in the existing `inv_track` app, and reduces to the
plain sheet formula when `margin_percent == 100`.

Mirrors the spreadsheet's own error handling: `IFERROR(NC/RC, 0)` falls back
to zero rather than raising.

Same-day (0-DTE) trades count as one day: DTE = MAX(1, EXP - TRADE DATE).
They used to be entered with a fake Saturday expiration so the sheet's
365/DTE wouldn't divide by zero; the true Friday expiration is stored now.
"""

from decimal import Decimal

ZERO = Decimal("0")
HUNDRED = Decimal("100")
DAYS_PER_YEAR = Decimal("365")


def calculate_rorc(
    net_credit_per_share: Decimal,
    risk_capital_per_share: Decimal,
    margin_percent: Decimal = HUNDRED,
) -> Decimal:
    """RORC = net_credit_per_share / (risk_capital_per_share * margin_percent / 100).

    Returns `Decimal("0")` if the effective risk capital is not positive,
    matching the spreadsheet's `IFERROR(NC/RC, 0)`.
    """
    effective_risk_capital = risk_capital_per_share * (margin_percent / HUNDRED)
    if effective_risk_capital <= ZERO:
        return ZERO
    return net_credit_per_share / effective_risk_capital


def effective_days_to_expiration(days_to_expiration: int) -> int:
    """Days used for annualizing: a same-day (0-DTE) trade counts as 1 day."""
    return max(days_to_expiration, 1)


def calculate_arorc(rorc: Decimal, days_to_expiration: int) -> Decimal:
    """ARORC = RORC * (365 / max(1, days_to_expiration)).

    Returns `Decimal("0")` for a negative `days_to_expiration` (expiration
    before the trade date is bad data, not a 0-DTE trade).
    """
    if days_to_expiration < 0:
        return ZERO
    multiplier = DAYS_PER_YEAR / Decimal(effective_days_to_expiration(days_to_expiration))
    return rorc * multiplier
