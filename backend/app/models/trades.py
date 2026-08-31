from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Trade(Base):
    """What you decided to do. Written once at entry. Never updated after that —
    everything that happens afterward (close, roll, expiration) is a `trade_events` row.
    """

    __tablename__ = "trades"
    __table_args__ = (
        Index("idx_trades_account", "account_id"),
        Index("idx_trades_ticker", "ticker_id"),
        Index("idx_trades_parent", "trade_parent_id"),
        UniqueConstraint("schwab_activity_id", name="uq_trades_schwab_activity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), nullable=False)
    # TODO: confirm whether this should stay as an intentional snapshot (protects
    # history if a ticker symbol is ever renamed) or be dropped in favor of always
    # joining tickers.ticker — see docs/DATA_MODEL.md §4.
    ticker: Mapped[str] = mapped_column(Text, nullable=False)
    trade_type_id: Mapped[int] = mapped_column(ForeignKey("trade_types.id"), nullable=False)
    # same TODO as above, for the type_name snapshot
    trade_type: Mapped[str] = mapped_column(Text, nullable=False)
    trade_parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("trades.id")
    )  # self-reference for rolls
    date_trade_open: Mapped[date] = mapped_column(Date, nullable=False)
    expiration_date: Mapped[date | None] = mapped_column(Date)
    days_to_expiration: Mapped[int | None] = mapped_column(Integer)
    num_of_contracts: Mapped[int | None] = mapped_column(Integer)
    num_of_shares: Mapped[int | None] = mapped_column(Integer)
    # Strikes are whole-cent dollar amounts (see alembic/versions/a2d04ed4d626).
    strike_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    long_strike: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    price_per_share: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    credit_debit: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    total_premium: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    commission_per_share: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    margin_capital: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    margin_percent: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    net_credit_per_share: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    risk_capital_per_share: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    arorc: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    schwab_order_id: Mapped[str | None] = mapped_column(Text)
    # Schwab activity id is the stable unique key when order id is missing.
    schwab_activity_id: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    needs_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Bookkeeping only, not part of the domain model — see
    # docs/PRODUCTION_IMPORT_RUNBOOK.md §4. Null for hand-entered trades.
    import_batch: Mapped[str | None] = mapped_column(Text, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
