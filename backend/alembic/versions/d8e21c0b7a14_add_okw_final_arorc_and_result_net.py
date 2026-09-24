"""store OKW FINAL ARORC and NET CREDIT/(DEBIT) on trades

Expand-only. Opening ARORC stays on `arorc`; the result-block FINAL ARORC
and dollar NET CREDIT/(DEBIT) are separate cells in OKW (rows 71 / 69).

Revision ID: d8e21c0b7a14
Revises: c4f8a1b2d903
Create Date: 2026-09-06 02:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8e21c0b7a14"
down_revision: Union[str, Sequence[str], None] = "c4f8a1b2d903"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trades", sa.Column("final_arorc", sa.Numeric(precision=10, scale=6), nullable=True))
    op.add_column(
        "trades",
        sa.Column("result_net_credit", sa.Numeric(precision=14, scale=2), nullable=True),
    )


def downgrade() -> None:
    """# BREAKING: drops imported OKW FINAL ARORC / NET CREDIT/(DEBIT)."""
    op.drop_column("trades", "result_net_credit")
    op.drop_column("trades", "final_arorc")
