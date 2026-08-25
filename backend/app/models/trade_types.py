from datetime import datetime

from sqlalchemy import Boolean, DateTime, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TradeType(Base):
    """Strategy registry — describes what fields each trade type needs, so adding a
    new strategy is a data insert, not a schema change.
    """

    __tablename__ = "trade_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 'ROCT PUT', 'BTO', 'STC', ...
    type_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text, nullable=False)  # 'OPTIONS' | 'STOCK'
    is_credit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    requires_expiration: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    requires_strike: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    requires_contracts: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    requires_shares: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
