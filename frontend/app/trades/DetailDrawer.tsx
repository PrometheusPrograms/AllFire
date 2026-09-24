import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { getTrade, type ChainEvent, type Trade, type TradeDetail } from "@/lib/api-client";
import FormattedDate from "./FormattedDate";
import OkwColumnSheet from "./OkwColumnSheet";
import { formatDate, formatMoney, formatShareCount, tradeShareCount } from "./format";
import styles from "./trades.module.css";

function rollKindLabel(kind: string | null | undefined): string | null {
  if (!kind) return null;
  return `${kind.charAt(0).toUpperCase()}${kind.slice(1)} roll`;
}

function eventHeadline(event: ChainEvent): string {
  if (event.event_type === "ROLL") {
    return rollKindLabel(event.roll_kind) ?? "ROLL";
  }
  return event.event_type;
}

function continuationIds(trade: TradeDetail): Set<number> {
  return new Set(
    trade.chain_legs.filter((leg) => leg.trade_parent_id != null).map((leg) => leg.id)
  );
}

function visibleTimeline(events: ChainEvent[], trade: TradeDetail): ChainEvent[] {
  const rolledInto = continuationIds(trade);
  return events.filter(
    (event) => !(event.event_type === "OPEN" && rolledInto.has(event.trade_id))
  );
}

function optionRightLabel(event: ChainEvent, column: Trade | undefined): string | null {
  const raw = event.option_right ?? column?.trade_type ?? null;
  if (!raw) return null;
  const upper = raw.toUpperCase();
  if (upper.includes("CALL")) return "call";
  if (upper.includes("PUT")) return "put";
  return raw.toLowerCase();
}

function eventSummary(event: ChainEvent, trade: TradeDetail) {
  const next =
    event.event_type === "ROLL" && event.next_trade_id != null
      ? trade.chain_legs.find((leg) => leg.id === event.next_trade_id)
      : undefined;
  const own = trade.chain_legs.find((leg) => leg.id === event.trade_id);
  const column = next ?? own;
  const tradeDate = next
    ? next.date_trade_open
    : event.event_type === "OPEN"
      ? (event.date_trade_open ?? event.event_date)
      : event.event_date;
  return {
    date: tradeDate,
    strike: next
      ? (event.to_strike ?? next.strike_price)
      : event.strike_price,
    expiration: next
      ? (event.to_expiration ?? next.expiration_date)
      : event.expiration_date,
    optionRight: optionRightLabel(event, column),
    shares: column ? tradeShareCount(column) : null,
    creditDebit: column?.credit_debit ?? null,
    tradeId: next?.id ?? event.trade_id,
  };
}

export default function DetailDrawer({
  tradeId,
  onClose,
}: {
  tradeId: number;
  onClose: () => void;
}) {
  const [trade, setTrade] = useState<TradeDetail | null>(null);
  const [focusedId, setFocusedId] = useState(tradeId);
  const [focusedEventId, setFocusedEventId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setTrade(null);
    getTrade(tradeId)
      .then((data) => {
        if (cancelled) return;
        setTrade(data);
        const ids = data.chain_trade_ids.length ? data.chain_trade_ids : [data.id];
        const tipId = ids[ids.length - 1];
        setFocusedId(tipId);
        const events = data.chain_events?.length ? data.chain_events : [];
        const shown = visibleTimeline(events, data);
        const tipEvent = [...shown].reverse().find((event) =>
          event.trade_id === tipId || event.next_trade_id === tipId
        );
        setFocusedEventId(tipEvent?.id ?? shown[0]?.id ?? null);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load trade.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tradeId]);

  const timeline = useMemo(() => {
    if (!trade) return [];
    const raw: ChainEvent[] = trade.chain_events?.length
      ? trade.chain_events
      : (trade.events ?? []).map((event) => ({
          ...event,
          trade_id: trade.id,
          strike_price: trade.strike_price,
          expiration_date: trade.expiration_date,
          date_trade_open: trade.date_trade_open,
          net_credit_per_share: trade.net_credit_per_share,
          arorc: trade.arorc,
          next_trade_id: null,
          roll_kind: null,
          option_right: null,
          to_strike: null,
          to_expiration: null,
        }));
    return visibleTimeline(raw, trade);
  }, [trade]);

  const focusedEvent = useMemo(
    () => timeline.find((event) => event.id === focusedEventId) ?? null,
    [timeline, focusedEventId]
  );

  const focused: Trade | null = useMemo(() => {
    if (!trade) return null;
    const fromEvent = focusedEvent
      ? trade.chain_legs.find(
          (leg) =>
            leg.id ===
            (focusedEvent.next_trade_id ?? focusedEvent.trade_id)
        )
      : null;
    return fromEvent ?? trade.chain_legs.find((leg) => leg.id === focusedId) ?? trade;
  }, [trade, focusedId, focusedEvent]);

  const navRefs = useRef<Map<number, HTMLButtonElement>>(new Map());

  useEffect(() => {
    if (focusedEventId == null) return;
    navRefs.current.get(focusedEventId)?.scrollIntoView({
      block: "nearest",
      behavior: "smooth",
    });
  }, [focusedEventId]);

  function selectEvent(event: ChainEvent) {
    setFocusedEventId(event.id);
    setFocusedId(event.next_trade_id ?? event.trade_id);
  }

  function selectLeg(legId: number) {
    setFocusedId(legId);
    const rollInto = timeline.find(
      (event) => event.event_type === "ROLL" && event.next_trade_id === legId
    );
    const own = timeline.find((event) => event.trade_id === legId);
    setFocusedEventId(rollInto?.id ?? own?.id ?? null);
  }

  const strikePath = trade?.strike_path
    ? trade.strike_path
        .split(" → ")
        .map((part) => formatMoney(part))
        .join(" → ")
    : null;

  const sheetLegs = trade?.chain_legs.length
    ? trade.chain_legs
    : focused
      ? [focused]
      : [];
  const visibleDataCols = Math.min(Math.max(sheetLegs.length, 1), 5);

  return (
    <div className={styles.overlay} onClick={onClose}>
      <div
        className={`${styles.drawer} ${styles.drawerFit}`}
        style={
          {
            "--okw-visible-cols": visibleDataCols,
            "--okw-scroll-gutter": sheetLegs.length > 5 ? "12px" : "0px",
          } as CSSProperties
        }
        onClick={(e) => e.stopPropagation()}
      >
        {loading && <div className={styles.loading}>Loading trade…</div>}
        {error && <div className={styles.error}>{error}</div>}
        {trade && focused && (
          <>
            <div className={styles.drawerHeader}>
              <div>
                <div className={styles.drawerTitle}>
                  {trade.ticker} — {trade.trade_type}
                </div>
                <div className={styles.drawerSubtitle}>
                  {trade.account_name} · Opened{" "}
                  <FormattedDate
                    value={trade.chain_legs[0]?.date_trade_open ?? trade.date_trade_open}
                  />
                  {trade.roll_legs > 1 ? ` · ${trade.roll_legs} columns` : ""}
                </div>
                {strikePath && <div className={styles.drawerSubtitle}>{strikePath}</div>}
              </div>
              <button type="button" className={styles.closeButton} onClick={onClose}>
                ✕
              </button>
            </div>

            <div className={styles.eventNav} role="listbox" aria-label="Event timeline">
              {timeline.map((event) => {
                const active = event.id === focusedEventId;
                const summary = eventSummary(event, trade);
                const headline = eventHeadline(event);
                const sharesLabel = formatShareCount(summary.shares);
                const strikeLabel = formatMoney(summary.strike);
                const creditLabel = formatMoney(summary.creditDebit);
                const ariaLabel = [
                  formatDate(summary.date),
                  headline,
                  sharesLabel,
                  formatDate(summary.expiration),
                  strikeLabel,
                  summary.optionRight,
                  `@ ${creditLabel}`,
                ]
                  .filter((part) => part && part !== "—")
                  .join(" ");
                return (
                  <button
                    key={event.id}
                    ref={(node) => {
                      if (node) navRefs.current.set(event.id, node);
                      else navRefs.current.delete(event.id);
                    }}
                    type="button"
                    role="option"
                    aria-selected={active}
                    aria-label={ariaLabel}
                    className={`${styles.eventNavRow} ${active ? styles.eventNavRowActive : ""}`}
                    onClick={() => selectEvent(event)}
                  >
                    <span className={styles.eventNavDate}>
                      <FormattedDate value={summary.date} />
                    </span>
                    <span className={styles.eventNavKind}>{headline}</span>
                    <span className={styles.eventNavShares}>{sharesLabel}</span>
                    <span className={styles.eventNavExp}>
                      <FormattedDate value={summary.expiration} />
                    </span>
                    <span className={styles.eventNavStrike}>{strikeLabel}</span>
                    <span className={styles.eventNavRight}>
                      {summary.optionRight ?? "—"}
                    </span>
                    <span className={styles.eventNavCredit}>@ {creditLabel}</span>
                  </button>
                );
              })}
            </div>

            <OkwColumnSheet
              legs={sheetLegs}
              focusedId={focused.id}
              onSelectLeg={selectLeg}
            />
          </>
        )}
      </div>
    </div>
  );
}
