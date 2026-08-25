"""Running cost-basis totals.

Mirrors `docs/DATA_MODEL.md` §3's `v_cost_basis_running` view — running
basis and running shares are computed here the same way the view computes
them at query time (a windowed running sum ordered by transaction date,
then id), so a corrected or backfilled row never leaves stale totals
downstream of it: each call takes the *prior* running totals and the new
row's contribution, and returns the new running totals.

Also matches the existing `inv_track` app's `create_cost_basis_entry`
logic: STC (sell to close) contributes a negative `total_amount` (proceeds
reduce basis) and a negative `shares` delta; BTO (buy to open) contributes
positive amounts for both.
"""

from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")


@dataclass(frozen=True)
class RunningCostBasis:
    running_basis: Decimal
    running_shares: Decimal
    basis_per_share: Decimal | None


def calculate_cost_basis(
    prior_running_basis: Decimal,
    prior_running_shares: Decimal,
    total_amount: Decimal,
    shares: Decimal,
) -> RunningCostBasis:
    """Fold one `cost_basis` row into the running totals.

    `basis_per_share` is `None` when `running_shares` is zero — division by
    zero is guarded rather than raised, per `docs/DATA_MODEL.md` §3's note
    to "guard the division by zero either way."
    """
    running_basis = prior_running_basis + total_amount
    running_shares = prior_running_shares + shares
    basis_per_share = running_basis / running_shares if running_shares != ZERO else None
    return RunningCostBasis(
        running_basis=running_basis,
        running_shares=running_shares,
        basis_per_share=basis_per_share,
    )
