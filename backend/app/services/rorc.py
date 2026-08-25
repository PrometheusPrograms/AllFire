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

Mirrors the spreadsheet's own error handling: `IFERROR(NC/RC, 0)` and
`IFERROR(365/DTE, 0)` both fall back to zero rather than raising, so these
functions do the same for non-positive denominators.
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


def calculate_arorc(rorc: Decimal, days_to_expiration: int) -> Decimal:
    """ARORC = RORC * (365 / days_to_expiration).

    Returns `Decimal("0")` if `days_to_expiration` is not positive, matching
    the spreadsheet's `IFERROR(365/DTE, 0)`.
    """
    if days_to_expiration <= 0:
        return ZERO
    multiplier = DAYS_PER_YEAR / Decimal(days_to_expiration)
    return rorc * multiplier
