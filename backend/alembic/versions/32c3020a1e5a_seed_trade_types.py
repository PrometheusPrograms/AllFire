"""seed trade types

Data-only migration — inserts the catalog rows for every trade type the
spreadsheet actually uses, pulled directly from row 1 of the real
`TRADES 2026` / `ROTH TRADES 2026` sheets (5 distinct option-strategy labels,
ticker prefix stripped), plus two plain-stock types (`BTO`/`STC`) requested
separately for trades that move cost basis without going through options.

`ROCT *` types trade *trading shares* (short-term, option-assignment-driven).
`RULE ONE *` types trade *long-term investment shares* (`RULE ONE CALL` is
used to sell long-term shares at/above full intrinsic value). This
trading-vs-long-term distinction is documented in each row's `description`
today; see docs/DATA_MODEL.md if/when it needs to become structured data for
the future portfolio-composition view.

Revision ID: 32c3020a1e5a
Revises: d5e684ca66b6
Create Date: 2026-08-28 16:35:11.186839

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '32c3020a1e5a'
down_revision: Union[str, Sequence[str], None] = 'd5e684ca66b6'
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
        "type_name": "ROCT PUT",
        "description": "Cash-secured put, ROCT strategy — trading shares (short-term).",
        "category": "OPTIONS",
        "is_credit": True,
        "requires_expiration": True,
        "requires_strike": True,
        "requires_contracts": True,
        "requires_shares": False,
    },
    {
        "type_name": "RULE ONE PUT",
        "description": "Cash-secured put, Rule One strategy — long-term investment shares.",
        "category": "OPTIONS",
        "is_credit": True,
        "requires_expiration": True,
        "requires_strike": True,
        "requires_contracts": True,
        "requires_shares": False,
    },
    {
        "type_name": "ROCT CALL",
        "description": "Covered call, ROCT strategy — trading shares (short-term).",
        "category": "OPTIONS",
        "is_credit": True,
        "requires_expiration": True,
        "requires_strike": True,
        "requires_contracts": True,
        "requires_shares": False,
    },
    {
        "type_name": "RULE ONE CALL",
        "description": (
            "Covered call, Rule One strategy — sells long-term investment shares "
            "at/above full intrinsic value."
        ),
        "category": "OPTIONS",
        "is_credit": True,
        "requires_expiration": True,
        "requires_strike": True,
        "requires_contracts": True,
        "requires_shares": False,
    },
    {
        "type_name": "ROCS BULL PUT SPREAD",
        "description": "Bull put credit spread — the only type using `long_strike`.",
        "category": "OPTIONS",
        "is_credit": True,
        "requires_expiration": True,
        "requires_strike": True,
        "requires_contracts": True,
        "requires_shares": False,
    },
    {
        "type_name": "BTO",
        "description": "Buy to open — plain stock purchase, no options; feeds cost basis directly.",
        "category": "STOCK",
        "is_credit": False,
        "requires_expiration": False,
        "requires_strike": False,
        "requires_contracts": False,
        "requires_shares": True,
    },
    {
        "type_name": "STC",
        "description": "Sell to close — plain stock sale, no options; proceeds reduce cost basis.",
        "category": "STOCK",
        "is_credit": True,
        "requires_expiration": False,
        "requires_strike": False,
        "requires_contracts": False,
        "requires_shares": True,
    },
]


def upgrade() -> None:
    """Insert the trade_types catalog rows."""
    op.bulk_insert(trade_types_table, SEED_ROWS)


def downgrade() -> None:
    """Remove exactly the rows this migration inserted, by name."""
    names = [row["type_name"] for row in SEED_ROWS]
    op.execute(
        trade_types_table.delete().where(trade_types_table.c.type_name.in_(names))
    )
