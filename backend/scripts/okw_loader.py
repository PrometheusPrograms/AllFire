"""Loads `ParsedTrade` records (see `okw_parser.py`) into the database.

Separated from parsing on purpose: `okw_parser.py` has zero DB dependency
and is unit-testable with a synthetic workbook; this module owns all the
DB-touching decisions (get-or-create, idempotency, what a `ParsedTrade`
turns into across `trades`/`trade_events`) and is exercised against a
throwaway SQLite/Postgres session in tests instead.

Deliberately out of scope for this first pass (see docs/PRODUCTION_IMPORT_RUNBOOK.md
and the plan this was built from): `cash_flows`, `cost_basis`, and `bankroll`
rows. Those require modeling real cash movements (assignment proceeds,
commissions actually charged) that this workbook's per-trade-block layout
doesn't cleanly hand us yet — better to under-import than guess wrong on
money movements. `trades`/`trade_events` alone already gets the display/UI
work (Phase 3+) real data to build against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, Ticker, Trade, TradeEvent, TradeType
from scripts.okw_parser import ParsedTrade

ZERO = Decimal("0")


class ImportBatchExistsError(Exception):
    """Raised when a batch tag has already been imported and --force wasn't passed."""


@dataclass
class ImportResult:
    trades_created: int = 0
    events_created: int = 0
    needs_review: int = 0
    skipped_no_ticker: list[str] = field(default_factory=list)
    unknown_trade_types: list[str] = field(default_factory=list)


def get_or_create_account(session: Session, account_name: str) -> Account:
    account = session.scalar(select(Account).where(Account.account_name == account_name))
    if account is not None:
        return account
    raise ValueError(
        f"Account {account_name!r} does not exist — create it first (accounts are "
        "few and long-lived; this loader deliberately doesn't guess start_date/"
        "starting_balance for a new one)."
    )


def get_or_create_ticker(session: Session, symbol: str) -> Ticker:
    symbol = symbol.strip().upper()
    ticker = session.scalar(select(Ticker).where(Ticker.ticker == symbol))
    if ticker is not None:
        return ticker
    ticker = Ticker(ticker=symbol)
    session.add(ticker)
    session.flush()
    return ticker


def get_trade_type(session: Session, type_name: str) -> TradeType | None:
    return session.scalar(select(TradeType).where(TradeType.type_name == type_name))


def batch_already_imported(session: Session, import_batch: str) -> bool:
    existing = session.scalar(select(Trade.id).where(Trade.import_batch == import_batch))
    return existing is not None


def delete_batch(session: Session, import_batch: str) -> None:
    """Remove every row a previous run of this exact batch created, so
    --force is a clean replace rather than a duplicate pile-up.
    """
    trade_ids = session.scalars(
        select(Trade.id).where(Trade.import_batch == import_batch)
    ).all()
    if not trade_ids:
        return
    session.query(TradeEvent).filter(TradeEvent.trade_id.in_(trade_ids)).delete(
        synchronize_session=False
    )
    session.query(Trade).filter(Trade.id.in_(trade_ids)).delete(synchronize_session=False)


def load_parsed_trades(
    session: Session,
    parsed_trades: list[ParsedTrade],
    *,
    account_name: str,
    import_batch: str,
    force: bool = False,
) -> ImportResult:
    if batch_already_imported(session, import_batch):
        if not force:
            raise ImportBatchExistsError(
                f"Batch {import_batch!r} was already imported. Pass --force to "
                "wipe and re-insert it."
            )
        delete_batch(session, import_batch)

    account = get_or_create_account(session, account_name)
    result = ImportResult()

    for parsed in parsed_trades:
        if not parsed.ticker:
            result.skipped_no_ticker.append(parsed.column_letter)
            continue

        trade_type = get_trade_type(session, parsed.trade_type_name)
        if trade_type is None:
            # trade_type_id is NOT NULL — an unrecognized type name means we
            # don't know required-field expectations for it either, so this
            # trade is skipped rather than inserted with a guessed type.
            # Add the type to trade_types first (Phase 1) if it's legitimate.
            result.unknown_trade_types.append(parsed.trade_type_name)
            continue
        needs_review = parsed.needs_review

        ticker = get_or_create_ticker(session, parsed.ticker)

        trade = Trade(
            account_id=account.id,
            ticker_id=ticker.id,
            ticker=ticker.ticker,
            trade_type_id=trade_type.id,
            trade_type=parsed.trade_type_name,
            date_trade_open=parsed.trade_date,
            expiration_date=parsed.expiration_date,
            days_to_expiration=parsed.days_to_expiration,
            num_of_contracts=parsed.num_of_contracts,
            num_of_shares=parsed.num_of_shares,
            strike_price=parsed.short_strike,
            long_strike=parsed.long_strike,
            price_per_share=parsed.price_per_share,
            credit_debit=parsed.credit_debit if parsed.credit_debit is not None else ZERO,
            commission_per_share=(
                parsed.commission_per_share
                if parsed.commission_per_share is not None
                else ZERO
            ),
            margin_capital=parsed.margin_capital,
            margin_percent=parsed.margin_percent,
            net_credit_per_share=parsed.net_credit_per_share,
            risk_capital_per_share=parsed.risk_capital_per_share,
            arorc=parsed.arorc,
            notes=parsed.notes,
            needs_review=needs_review,
            import_batch=import_batch,
        )
        session.add(trade)
        session.flush()  # need trade.id for the OPEN event below
        result.trades_created += 1
        if needs_review:
            result.needs_review += 1

        session.add(
            TradeEvent(
                trade_id=trade.id,
                event_type="OPEN",
                event_date=parsed.trade_date,
                import_batch=import_batch,
            )
        )
        result.events_created += 1

        if parsed.event_type:
            session.add(
                TradeEvent(
                    trade_id=trade.id,
                    event_type=parsed.event_type,
                    event_date=parsed.result_date or parsed.trade_date,
                    closing_debit=parsed.closing_debit,
                    total_debit=parsed.total_debit,
                    notes=(
                        f"Imported RESULT={parsed.result_raw!r}" if parsed.result_raw else None
                    ),
                    import_batch=import_batch,
                )
            )
            result.events_created += 1

    return result
