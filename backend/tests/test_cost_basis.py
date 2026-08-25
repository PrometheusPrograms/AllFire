"""Tests for app.services.cost_basis."""

from decimal import Decimal

from app.services.cost_basis import RunningCostBasis, calculate_cost_basis


def test_cost_basis_first_entry():
    result = calculate_cost_basis(
        prior_running_basis=Decimal("0"),
        prior_running_shares=Decimal("0"),
        total_amount=Decimal("1000.00"),
        shares=Decimal("100"),
    )
    assert result == RunningCostBasis(
        running_basis=Decimal("1000.00"),
        running_shares=Decimal("100"),
        basis_per_share=Decimal("10.00"),
    )


def test_cost_basis_accumulates_across_calls():
    first = calculate_cost_basis(
        prior_running_basis=Decimal("0"),
        prior_running_shares=Decimal("0"),
        total_amount=Decimal("1000.00"),
        shares=Decimal("100"),
    )
    second = calculate_cost_basis(
        prior_running_basis=first.running_basis,
        prior_running_shares=first.running_shares,
        total_amount=Decimal("550.00"),
        shares=Decimal("50"),
    )
    assert second.running_basis == Decimal("1550.00")
    assert second.running_shares == Decimal("150")
    assert second.basis_per_share == Decimal("1550.00") / Decimal("150")


def test_cost_basis_stc_reduces_shares_and_basis():
    # STC (sell to close): negative total_amount (proceeds reduce basis)
    # and negative shares delta, per the existing app's create_cost_basis_entry.
    opening = calculate_cost_basis(
        prior_running_basis=Decimal("0"),
        prior_running_shares=Decimal("0"),
        total_amount=Decimal("1000.00"),
        shares=Decimal("100"),
    )
    closing = calculate_cost_basis(
        prior_running_basis=opening.running_basis,
        prior_running_shares=opening.running_shares,
        total_amount=Decimal("-400.00"),
        shares=Decimal("-40"),
    )
    assert closing.running_basis == Decimal("600.00")
    assert closing.running_shares == Decimal("60")
    assert closing.basis_per_share == Decimal("10.00")


def test_cost_basis_zero_running_shares_guards_division():
    result = calculate_cost_basis(
        prior_running_basis=Decimal("500.00"),
        prior_running_shares=Decimal("50"),
        total_amount=Decimal("-500.00"),
        shares=Decimal("-50"),
    )
    assert result.running_shares == Decimal("0")
    assert result.basis_per_share is None


def test_cost_basis_a_corrected_backfilled_row_does_not_use_stale_totals():
    # Calling with the same prior totals twice (e.g. re-deriving from a
    # corrected upstream row) always produces the same result — there is
    # no mutable stored state for a correction to leave stale.
    prior_basis = Decimal("2000.00")
    prior_shares = Decimal("200")

    original = calculate_cost_basis(prior_basis, prior_shares, Decimal("100.00"), Decimal("10"))
    corrected = calculate_cost_basis(prior_basis, prior_shares, Decimal("150.00"), Decimal("10"))

    assert original.running_basis == Decimal("2100.00")
    assert corrected.running_basis == Decimal("2150.00")
