"""round strike prices to two decimals

Strikes are always whole-cent dollar amounts, unlike net_credit_per_share /
commission_per_share (which genuinely need finer precision). Rounds any
existing data first (ROUND, not truncate, so this matches how the values
were already displayed) then narrows the column scale — this ordering
means the ALTER never fails on an out-of-range value.

Revision ID: a2d04ed4d626
Revises: 823df6e0a08f
Create Date: 2026-08-29 14:24:09.830972

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a2d04ed4d626'
down_revision: Union[str, Sequence[str], None] = '823df6e0a08f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE trades SET strike_price = ROUND(strike_price, 2) WHERE strike_price IS NOT NULL")
    op.execute("UPDATE trades SET long_strike = ROUND(long_strike, 2) WHERE long_strike IS NOT NULL")
    op.alter_column(
        "trades", "strike_price", type_=sa.Numeric(12, 2), existing_type=sa.Numeric(12, 4)
    )
    op.alter_column(
        "trades", "long_strike", type_=sa.Numeric(12, 2), existing_type=sa.Numeric(12, 4)
    )


def downgrade() -> None:
    op.alter_column(
        "trades", "strike_price", type_=sa.Numeric(12, 4), existing_type=sa.Numeric(12, 2)
    )
    op.alter_column(
        "trades", "long_strike", type_=sa.Numeric(12, 4), existing_type=sa.Numeric(12, 2)
    )
