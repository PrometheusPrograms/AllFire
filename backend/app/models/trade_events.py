from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Numeric, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

EVENT_TYPES = ("OPEN", "ROLL", "ADJUST", "CLOSE", "EXPIRE", "ASSIGN")


class TradeEvent(Base):
    """Append-only lifecycle log. This is the entire answer to "immutable history":
    a trade's status, close date, and closing debit are never columns on `trades`
    you overwrite — they're rows here that accumulate, in order, forever.

    Deliberately no `updated_at`, no soft-delete flag: rows are never modified
    after insert. If a mistake needs correcting, insert a new ADJUST row that
    says so in notes — don't edit the old one.
    """

    __tablename__ = "trade_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('OPEN','ROLL','ADJUST','CLOSE','EXPIRE','ASSIGN')",
            name="ck_trade_events_event_type",
        ),
        Index("idx_trade_events_trade", "trade_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    closing_debit: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    total_debit: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    schwab_order_id: Mapped[str | None] = mapped_column(
        Text
    )  # the closing/rolling order, distinct from the opening one on `trades`
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
