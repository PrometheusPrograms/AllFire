"""add import_batch to trades and trade_events

Additive/expand-only column per docs/PRODUCTION_IMPORT_RUNBOOK.md §4 —
bookkeeping for the spreadsheet import, not part of the domain model. Tags
every row an import run creates so a re-run of the same batch can be
detected and refused (or explicitly replaced with --force), and so a bad
import can be identified/rolled back without touching hand-entered trades.

Revision ID: 823df6e0a08f
Revises: 32c3020a1e5a
Create Date: 2026-08-28 16:35:37.084339

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '823df6e0a08f'
down_revision: Union[str, Sequence[str], None] = '32c3020a1e5a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add nullable import_batch columns — safe, additive, no backfill needed."""
    op.add_column("trades", sa.Column("import_batch", sa.Text(), nullable=True))
    op.add_column("trade_events", sa.Column("import_batch", sa.Text(), nullable=True))
    op.create_index("idx_trades_import_batch", "trades", ["import_batch"])
    op.create_index("idx_trade_events_import_batch", "trade_events", ["import_batch"])


def downgrade() -> None:
    """Drop the import_batch columns.

    # BREAKING: drops import_batch data — only safe if no import history needs
    # to be traced back to its source batch anymore.
    """
    op.drop_index("idx_trade_events_import_batch", table_name="trade_events")
    op.drop_index("idx_trades_import_batch", table_name="trades")
    op.drop_column("trade_events", "import_batch")
    op.drop_column("trades", "import_batch")
