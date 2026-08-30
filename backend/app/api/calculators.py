"""Calculator routes — thin HTTP wrappers around the pure functions in
`app.services`, so the RORC/ARORC/Kelly/cost-basis math can be exercised
interactively (e.g. from the frontend's `/calculators` page) without any
persistence or other business logic attached.

Deliberately scoped to just these four calculators — trades/events/other
resource routes live in their own `app/api/*.py` modules.
"""

from decimal import Decimal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.cost_basis import calculate_cost_basis
from app.services.kelly import calculate_kelly
from app.services.rorc import calculate_arorc, calculate_rorc

router = APIRouter(prefix="/api/calculators", tags=["calculators"])


class RorcRequest(BaseModel):
    net_credit_per_share: Decimal
    risk_capital_per_share: Decimal
    margin_percent: Decimal = Field(default=Decimal("100"))


class RorcResponse(BaseModel):
    rorc: Decimal


@router.post("/rorc", response_model=RorcResponse)
def rorc(payload: RorcRequest) -> RorcResponse:
    result = calculate_rorc(
        net_credit_per_share=payload.net_credit_per_share,
        risk_capital_per_share=payload.risk_capital_per_share,
        margin_percent=payload.margin_percent,
    )
    return RorcResponse(rorc=result)


class ArorcRequest(BaseModel):
    rorc: Decimal
    days_to_expiration: int


class ArorcResponse(BaseModel):
    arorc: Decimal


@router.post("/arorc", response_model=ArorcResponse)
def arorc(payload: ArorcRequest) -> ArorcResponse:
    result = calculate_arorc(
        rorc=payload.rorc,
        days_to_expiration=payload.days_to_expiration,
    )
    return ArorcResponse(arorc=result)


class KellyRequest(BaseModel):
    probability_of_winning: Decimal
    avg_win: Decimal
    avg_loss: Decimal


class KellyResponse(BaseModel):
    kelly: Decimal


@router.post("/kelly", response_model=KellyResponse)
def kelly(payload: KellyRequest) -> KellyResponse:
    result = calculate_kelly(
        probability_of_winning=payload.probability_of_winning,
        avg_win=payload.avg_win,
        avg_loss=payload.avg_loss,
    )
    return KellyResponse(kelly=result)


class CostBasisRequest(BaseModel):
    prior_running_basis: Decimal
    prior_running_shares: Decimal
    total_amount: Decimal
    shares: Decimal


class CostBasisResponse(BaseModel):
    running_basis: Decimal
    running_shares: Decimal
    basis_per_share: Decimal | None


@router.post("/cost-basis", response_model=CostBasisResponse)
def cost_basis(payload: CostBasisRequest) -> CostBasisResponse:
    result = calculate_cost_basis(
        prior_running_basis=payload.prior_running_basis,
        prior_running_shares=payload.prior_running_shares,
        total_amount=payload.total_amount,
        shares=payload.shares,
    )
    return CostBasisResponse(
        running_basis=result.running_basis,
        running_shares=result.running_shares,
        basis_per_share=result.basis_per_share,
    )
