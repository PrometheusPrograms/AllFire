"""seed bull put spread trade type

Data-only migration — adds `BULL PUT SPREAD` as its own catalog row, distinct
from `ROCS BULL PUT SPREAD`. Discovered importing the 2024/2025 historical
workbooks: those years traded plain index (RUT) bull put spreads under this
older name, before the `ROCS` naming convention (real-stock bull put
spreads) was adopted. Per user confirmation, these are a different strategy
and must not be merged into `ROCS BULL PUT SPREAD`.

Revision ID: f5af35fbedea
Revises: a2d04ed4d626
Create Date: 2026-08-29 23:17:38.138229

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f5af35fbedea'
down_revision: Union[str, Sequence[str], None] = 'a2d04ed4d626'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


trade_types_table = sa.table(
    "trade_types",
    sa.column("type_name", sa.Text),
    sa.column("description", sa.Text),
    sa.column("category", sa.Text),
    sa.column("is_credit", sa.Boolean),
    sa.column("requires_expiration", sa.Boolean),
    sa.column("requires_strike", sa.Boolean),
    sa.column("requires_contracts", sa.Boolean),
    sa.column("requires_shares", sa.Boolean),
)

SEED_ROWS = [
    {
        "type_name": "BULL PUT SPREAD",
        "description": (
            "Bull put credit spread on an index (RUT) — an older/separate strategy "
            "from ROCS BULL PUT SPREAD (real-stock spreads); uses `long_strike` too."
        ),
        "category": "OPTIONS",
        "is_credit": True,
        "requires_expiration": True,
        "requires_strike": True,
        "requires_contracts": True,
        "requires_shares": False,
    },
]


def upgrade() -> None:
    """Insert the BULL PUT SPREAD trade_types catalog row."""
    op.bulk_insert(trade_types_table, SEED_ROWS)


def downgrade() -> None:
    """Remove exactly the row this migration inserted, by name."""
    names = [row["type_name"] for row in SEED_ROWS]
    op.execute(
        trade_types_table.delete().where(trade_types_table.c.type_name.in_(names))
    )
