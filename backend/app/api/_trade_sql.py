"""SQL expressions shared by the trade/analytics/position routes."""

from sqlalchemy import func, select

from app.models import Trade, TradeEvent

# A trade's signed closing debit in dollars, as OKW books it: `TOTAL DEBIT` is
# negative when a close or roll cost money (GOOG col WZ: -35). Summed over the
# trade's closing events; NULL when it has none.
trade_total_debit = (
    select(func.sum(TradeEvent.total_debit))
    .where(
        TradeEvent.trade_id == Trade.id,
        TradeEvent.event_type.in_(("CLOSE", "EXPIRE", "ASSIGN", "ROLL")),
    )
    .correlate(Trade)
    .scalar_subquery()
)
