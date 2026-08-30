import { useEffect, useState } from "react";
import { getTrade, type TradeDetail } from "@/lib/api-client";
import { formatDate, formatMoney, formatPercent } from "./format";
import styles from "./trades.module.css";

export default function DetailDrawer({
  tradeId,
  onClose,
}: {
  tradeId: number;
  onClose: () => void;
}) {
  const [trade, setTrade] = useState<TradeDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setTrade(null);
    getTrade(tradeId)
      .then((data) => {
        if (!cancelled) setTrade(data);
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

  return (
    <div className={styles.overlay} onClick={onClose}>
      <div className={styles.drawer} onClick={(e) => e.stopPropagation()}>
        {loading && <div className={styles.loading}>Loading trade…</div>}
        {error && <div className={styles.error}>{error}</div>}
        {trade && (
          <>
            <div className={styles.drawerHeader}>
              <div>
                <div className={styles.drawerTitle}>
                  {trade.ticker} — {trade.trade_type}
                </div>
                <div className={styles.drawerSubtitle}>
                  {trade.account_name} · Opened {formatDate(trade.date_trade_open)}
                </div>
              </div>
              <button type="button" className={styles.closeButton} onClick={onClose}>
                ✕
              </button>
            </div>

            <div className={styles.detailGrid}>
              <DetailItem label="Strike" value={trade.strike_price ?? "—"} />
              <DetailItem label="Long strike" value={trade.long_strike ?? "—"} />
              <DetailItem label="Expiration" value={formatDate(trade.expiration_date)} />
              <DetailItem label="Days to expiration" value={trade.days_to_expiration ?? "—"} />
              <DetailItem label="Contracts" value={trade.num_of_contracts ?? "—"} />
              <DetailItem label="Shares" value={trade.num_of_shares ?? "—"} />
              <DetailItem label="Credit/debit" value={formatMoney(trade.credit_debit)} />
              <DetailItem
                label="Net credit/share"
                value={formatMoney(trade.net_credit_per_share)}
              />
              <DetailItem
                label="Risk capital/share"
                value={formatMoney(trade.risk_capital_per_share)}
              />
              <DetailItem label="Margin %" value={formatPercent(trade.margin_percent)} />
              <DetailItem label="ARORC" value={formatPercent(trade.arorc)} />
              <DetailItem
                label="Running basis"
                value={trade.running_basis !== null ? formatMoney(trade.running_basis) : "— (not yet populated)"}
              />
              <DetailItem
                label="Running shares"
                value={trade.running_shares ?? "—"}
              />
            </div>

            <div className={styles.sectionTitle}>Event timeline</div>
            <div className={styles.timeline}>
              {trade.events.map((event) => (
                <div key={event.id} className={styles.timelineItem}>
                  <div className={styles.timelineType}>{event.event_type}</div>
                  <div className={styles.timelineDate}>{formatDate(event.event_date)}</div>
                  {event.closing_debit !== null && (
                    <div className={styles.timelineNotes}>
                      Closing debit: {formatMoney(event.closing_debit)}
                    </div>
                  )}
                  {event.notes && <div className={styles.timelineNotes}>{event.notes}</div>}
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function DetailItem({ label, value }: { label: string; value: string | number }) {
  return (
    <div className={styles.detailItem}>
      <span className={styles.detailLabel}>{label}</span>
      <span className={styles.detailValue}>{value}</span>
    </div>
  );
}
