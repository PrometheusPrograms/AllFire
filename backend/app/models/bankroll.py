from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Bankroll(Base):
    __tablename__ = "bankroll"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    transaction_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False
    )  # signed: deposit positive, withdrawal negative
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
