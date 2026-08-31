"""add schwab activity ids for incremental import

Expand-only: unique Schwab activity keys so the CLI importer can skip
already-written dividends/BTOs without wiping a batch. Also adds
cash_flows.import_batch for the same bookkeeping trades already have.

Revision ID: 9f3c2a1b8e07
Revises: f5af35fbedea
Create Date: 2026-08-31 00:10:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9f3c2a1b8e07"
down_revision: Union[str, Sequence[str], None] = "f5af35fbedea"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cash_flows", sa.Column("schwab_activity_id", sa.Text(), nullable=True))
    op.add_column("cash_flows", sa.Column("import_batch", sa.Text(), nullable=True))
    op.create_index("ix_cash_flows_import_batch", "cash_flows", ["import_batch"])
    op.create_unique_constraint(
        "uq_cash_flows_schwab_activity_id", "cash_flows", ["schwab_activity_id"]
    )

    op.add_column("trades", sa.Column("schwab_activity_id", sa.Text(), nullable=True))
    op.create_unique_constraint(
        "uq_trades_schwab_activity_id", "trades", ["schwab_activity_id"]
    )


def downgrade() -> None:
    """Drop Schwab activity keys.

    # BREAKING: drops schwab_activity_id / cash_flows.import_batch — only safe
    # if no Schwab-imported rows need those identifiers for idempotency.
    """
    op.drop_constraint("uq_trades_schwab_activity_id", "trades", type_="unique")
    op.drop_column("trades", "schwab_activity_id")
    op.drop_constraint("uq_cash_flows_schwab_activity_id", "cash_flows", type_="unique")
    op.drop_index("ix_cash_flows_import_batch", table_name="cash_flows")
    op.drop_column("cash_flows", "import_batch")
    op.drop_column("cash_flows", "schwab_activity_id")
