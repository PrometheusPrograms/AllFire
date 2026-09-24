"""Classify Schwab transaction JSON into dividends, equity buys, or review.

Pure: no HTTP, no DB. Money values are `Decimal`. Unit-tested against
anonymized fixtures in `tests/fixtures/schwab_transactions.json`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0")
EQUITY_TYPES = {"EQUITY", "COLLECTIVE_INVESTMENT", "MUTUAL_FUND"}
OPTION_TYPES = {"OPTION"}


@dataclass(frozen=True)
class CashDividend:
    activity_id: str
    transaction_date: date
    amount: Decimal
    ticker: str | None
    description: str | None


@dataclass(frozen=True)
class EquityBuy:
    activity_id: str
    order_id: str | None
    transaction_date: date
    ticker: str
    shares: int
    price: Decimal
    fees: Decimal
    description: str | None


@dataclass(frozen=True)
class EquitySell:
    activity_id: str
    order_id: str | None
    transaction_date: date
    ticker: str
    shares: int
    price: Decimal
    fees: Decimal
    description: str | None


@dataclass(frozen=True)
class ReviewItem:
    activity_id: str
    reason: str
    description: str | None


@dataclass
class ParseResult:
    dividends: list[CashDividend] = field(default_factory=list)
    equity_buys: list[EquityBuy] = field(default_factory=list)
    equity_sells: list[EquitySell] = field(default_factory=list)
    review: list[ReviewItem] = field(default_factory=list)


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _as_str_id(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _parse_date(raw: Any) -> date | None:
    if raw is None or raw == "":
        return None
    text = str(raw)
    # Schwab uses ISO-8601, sometimes without a colon in the timezone offset.
    if text.endswith("+0000"):
        text = text[:-5] + "+00:00"
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.date()


def _activity_date(txn: dict[str, Any]) -> date | None:
    return _parse_date(txn.get("tradeDate")) or _parse_date(txn.get("time"))


def _description(txn: dict[str, Any]) -> str | None:
    value = txn.get("description")
    return str(value) if value else None


def _blob(*parts: str | None) -> str:
    return " ".join(p for p in parts if p).upper()


def _instrument_type(item: dict[str, Any]) -> str:
    instrument = item.get("instrument") or {}
    return str(instrument.get("assetType") or "").upper()


def _instrument_symbol(item: dict[str, Any]) -> str | None:
    instrument = item.get("instrument") or {}
    symbol = instrument.get("symbol") or instrument.get("underlyingSymbol")
    if not symbol:
        return None
    return str(symbol).strip().upper()


def _is_fee_item(item: dict[str, Any]) -> bool:
    fee_type = str(item.get("feeType") or "").upper()
    return bool(fee_type) and fee_type != "NONE"


def _equity_items(txn: dict[str, Any]) -> list[dict[str, Any]]:
    items = txn.get("transferItems") or []
    return [
        item for item in items if _instrument_type(item) in EQUITY_TYPES and not _is_fee_item(item)
    ]


def _option_items(txn: dict[str, Any]) -> list[dict[str, Any]]:
    items = txn.get("transferItems") or []
    return [item for item in items if _instrument_type(item) in OPTION_TYPES]


def _fee_total(txn: dict[str, Any]) -> Decimal:
    total = ZERO
    for item in txn.get("transferItems") or []:
        if not _is_fee_item(item):
            continue
        cost = _as_decimal(item.get("cost"))
        if cost is not None:
            total += abs(cost)
    return total


def _share_qty(item: dict[str, Any]) -> Decimal:
    amount = _as_decimal(item.get("amount")) or ZERO
    return amount


def _whole_shares(qty: Decimal) -> int:
    """Nearest whole share. `int(Decimal)` truncates toward zero, which turned
    LULU 9.9-share API fills into 9-share BTOs against 10-share statements."""
    return int(abs(qty).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _looks_like_interest(txn: dict[str, Any]) -> bool:
    desc = (_description(txn) or "").upper()
    activity = str(txn.get("activityType") or "").upper()
    # Do not use `type` — Schwab's DIVIDEND_OR_INTEREST always contains "DIVIDEND".
    blob = _blob(desc, activity)
    if "DIVIDEND" in blob:
        return False
    return "INTEREST" in blob


def _looks_like_drip(txn: dict[str, Any], equity: list[dict[str, Any]]) -> bool:
    blob = _blob(_description(txn), str(txn.get("type") or ""))
    if "DRIP" in blob or "REINVEST" in blob:
        return True
    shares = sum((_share_qty(item) for item in equity), ZERO)
    return shares != ZERO and str(txn.get("type") or "").upper() in {
        "DIVIDEND_OR_INTEREST",
        "DIVIDEND",
    }


def _looks_like_assignment(txn: dict[str, Any]) -> bool:
    blob = _blob(_description(txn), str(txn.get("type") or ""), str(txn.get("activityType") or ""))
    return any(word in blob for word in ("ASSIGN", "EXERCISE", "EXPIRED", "REDEMPTION"))


def _instruction(item: dict[str, Any]) -> str:
    return str(item.get("instruction") or item.get("positionEffect") or "").upper()


def classify_transactions(transactions: list[dict[str, Any]]) -> ParseResult:
    result = ParseResult()
    for txn in transactions:
        activity_id = _as_str_id(txn.get("activityId") or txn.get("activity_id"))
        if not activity_id:
            result.review.append(
                ReviewItem(
                    activity_id="",
                    reason="missing_activity_id",
                    description=_description(txn),
                )
            )
            continue
        txn_date = _activity_date(txn)
        if txn_date is None:
            result.review.append(
                ReviewItem(
                    activity_id=activity_id,
                    reason="unparseable_date",
                    description=_description(txn),
                )
            )
            continue

        txn_type = str(txn.get("type") or "").upper()
        equity = _equity_items(txn)
        options = _option_items(txn)
        desc = _description(txn)

        if options:
            result.review.append(
                ReviewItem(
                    activity_id=activity_id,
                    reason="option_leg",
                    description=desc,
                )
            )
            continue

        if _looks_like_assignment(txn):
            result.review.append(
                ReviewItem(
                    activity_id=activity_id,
                    reason="assignment_or_exercise",
                    description=desc,
                )
            )
            continue

        if txn_type in {"DIVIDEND_OR_INTEREST", "DIVIDEND"} or (
            desc and "DIVIDEND" in desc.upper()
        ):
            if _looks_like_interest(txn):
                result.review.append(
                    ReviewItem(activity_id=activity_id, reason="interest", description=desc)
                )
                continue
            if _looks_like_drip(txn, equity):
                result.review.append(
                    ReviewItem(activity_id=activity_id, reason="drip", description=desc)
                )
                continue
            net = _as_decimal(txn.get("netAmount"))
            if net is None or net <= ZERO:
                result.review.append(
                    ReviewItem(
                        activity_id=activity_id,
                        reason="non_cash_or_negative_dividend",
                        description=desc,
                    )
                )
                continue
            ticker = _instrument_symbol(equity[0]) if equity else None
            result.dividends.append(
                CashDividend(
                    activity_id=activity_id,
                    transaction_date=txn_date,
                    amount=net,
                    ticker=ticker,
                    description=desc,
                )
            )
            continue

        if txn_type == "TRADE":
            if len(equity) != 1:
                result.review.append(
                    ReviewItem(
                        activity_id=activity_id,
                        reason="trade_not_single_equity",
                        description=desc,
                    )
                )
                continue
            item = equity[0]
            shares = _share_qty(item)
            instruction = _instruction(item)
            is_sell = shares < ZERO or instruction in {"SELL", "CLOSING"}
            is_buy = shares > ZERO or instruction in {"BUY", "OPENING"}
            if is_sell and not is_buy:
                ticker = _instrument_symbol(item)
                price = _as_decimal(item.get("price")) or ZERO
                sell_shares = abs(shares)
                if not ticker or sell_shares <= ZERO or price <= ZERO:
                    result.review.append(
                        ReviewItem(
                            activity_id=activity_id,
                            reason="incomplete_equity_sell",
                            description=desc,
                        )
                    )
                    continue
                fees = _fee_total(txn)
                share_count = _whole_shares(sell_shares)
                if fees == ZERO:
                    net = _as_decimal(txn.get("netAmount"))
                    if net is not None:
                        implied = (share_count * price) - net
                        if implied > Decimal("0.005"):
                            fees = implied
                order_id = _as_str_id(txn.get("orderId") or txn.get("order_id")) or None
                result.equity_sells.append(
                    EquitySell(
                        activity_id=activity_id,
                        order_id=order_id,
                        transaction_date=txn_date,
                        ticker=ticker,
                        shares=share_count,
                        price=price,
                        fees=fees,
                        description=desc,
                    )
                )
                continue
            if not is_buy:
                result.review.append(
                    ReviewItem(
                        activity_id=activity_id,
                        reason="equity_trade_not_buy",
                        description=desc,
                    )
                )
                continue
            ticker = _instrument_symbol(item)
            price = _as_decimal(item.get("price")) or ZERO
            if not ticker or shares <= ZERO or price <= ZERO:
                result.review.append(
                    ReviewItem(
                        activity_id=activity_id,
                        reason="incomplete_equity_buy",
                        description=desc,
                    )
                )
                continue
            fees = _fee_total(txn)
            share_count = _whole_shares(shares)
            if fees == ZERO:
                net = _as_decimal(txn.get("netAmount"))
                if net is not None:
                    implied = abs(net) - (abs(shares) * price)
                    if implied > Decimal("0.005"):
                        fees = implied
            order_id = _as_str_id(txn.get("orderId") or txn.get("order_id")) or None
            result.equity_buys.append(
                EquityBuy(
                    activity_id=activity_id,
                    order_id=order_id,
                    transaction_date=txn_date,
                    ticker=ticker,
                    shares=share_count,
                    price=price,
                    fees=fees,
                    description=desc,
                )
            )
            continue

        result.review.append(
            ReviewItem(
                activity_id=activity_id,
                reason=f"unsupported_type:{txn_type or 'unknown'}",
                description=desc,
            )
        )
    return result
