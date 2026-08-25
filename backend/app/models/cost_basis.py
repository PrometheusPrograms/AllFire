from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Numeric, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CostBasis(Base):
    """Append-only ledger of basis-affecting transactions. Running totals
    (`running_basis`, `running_shares`) are deliberately not stored here — they're
    computed at query time by `v_cost_basis_running`, so a corrected or backfilled
    row can never leave stale totals downstream of it.
    """

    __tablename__ = "cost_basis"
    __table_args__ = (
        Index(
            "idx_cost_basis_ticker_account",
            "account_id",
            "ticker_id",
            "transaction_date",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), nullable=False)
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"))
    cash_flow_id: Mapped[int | None] = mapped_column(ForeignKey("cash_flows.id"))
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    shares: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_per_share: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
