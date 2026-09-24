"""add OKW delta and probability of winning on trades

Expand-only: these sit on the original column in OKW (rows 21–22) but were
never imported. Nullable so existing rows stay valid until
`scripts/backfill_okw_column_fields.py` (or a new import) fills them.

Revision ID: c4f8a1b2d903
Revises: 9f3c2a1b8e07
Create Date: 2026-09-06 02:10:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4f8a1b2d903"
down_revision: Union[str, Sequence[str], None] = "9f3c2a1b8e07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trades", sa.Column("delta", sa.Numeric(precision=8, scale=4), nullable=True))
    op.add_column(
        "trades",
        sa.Column("probability_of_winning", sa.Numeric(precision=8, scale=6), nullable=True),
    )


def downgrade() -> None:
    """# BREAKING: drops imported OKW delta / probability of winning."""
    op.drop_column("trades", "probability_of_winning")
    op.drop_column("trades", "delta")
