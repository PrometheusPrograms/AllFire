from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CashFlow(Base):
    __tablename__ = "cash_flows"
    __table_args__ = (
        Index("idx_cash_flows_trade", "trade_id"),
        UniqueConstraint("schwab_activity_id", name="uq_cash_flows_schwab_activity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    ticker_id: Mapped[int | None] = mapped_column(ForeignKey("tickers.id"))
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"))
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    # 'SELL PUT', 'BUY TO CLOSE', 'DIVIDEND', ...
    transaction_type: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    schwab_activity_id: Mapped[str | None] = mapped_column(Text)
    # Bookkeeping only — see docs/SCHWAB_IMPORT.md. Null for non-Schwab rows.
    import_batch: Mapped[str | None] = mapped_column(Text, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
