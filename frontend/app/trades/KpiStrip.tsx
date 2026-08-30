import type { AnalyticsSummary } from "@/lib/api-client";
import { formatMoney, formatPercent } from "./format";
import styles from "./trades.module.css";

function KpiCard({
  label,
  value,
  subvalue,
  attention,
  onClick,
}: {
  label: string;
  value: string;
  subvalue?: string;
  attention?: boolean;
  onClick?: () => void;
}) {
  const classNames = [styles.kpiCard, attention && styles.kpiCardAttention, onClick && styles.kpiCardClickable]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      className={classNames}
      onClick={onClick}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
    >
      <span className={styles.kpiLabel}>{label}</span>
      <span className={styles.kpiValue}>{value}</span>
      {subvalue && <span className={styles.kpiSubvalue}>{subvalue}</span>}
    </div>
  );
}

export default function KpiStrip({
  summary,
  loading,
  onSelectNeedsReview,
  onSelectExpiringThisWeek,
  totalPremiumInRange,
  totalPremiumRangeLabel,
}: {
  summary: AnalyticsSummary | null;
  loading: boolean;
  onSelectNeedsReview: () => void;
  onSelectExpiringThisWeek: () => void;
  totalPremiumInRange: string | null;
  totalPremiumRangeLabel: string;
}) {
  if (loading || !summary) {
    return (
      <div className={styles.kpiGrid}>
        {Array.from({ length: 7 }).map((_, i) => (
          <div key={i} className={styles.kpiCard}>
            <span className={styles.kpiLabel}>Loading…</span>
            <span className={styles.kpiValue}>—</span>
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className={styles.kpiGrid}>
      <KpiCard label="Open trades" value={String(summary.open_trades_count)} />
      <KpiCard label="Premium this week" value={formatMoney(summary.premium_this_week)} />
      <KpiCard
        label="Avg weekly premium (YTD)"
        value={formatMoney(summary.avg_weekly_premium_ytd)}
      />
      <KpiCard
        label={`Total premium (${totalPremiumRangeLabel})`}
        value={formatMoney(totalPremiumInRange)}
      />
      <KpiCard label="Avg ARORC (open)" value={formatPercent(summary.avg_arorc_open)} />
      <KpiCard
        label="Expiring this week"
        value={String(summary.upcoming_expirations_7d_count)}
        subvalue={summary.upcoming_expirations_7d
          .slice(0, 3)
          .map((e) => e.ticker)
          .join(", ")}
        attention={summary.upcoming_expirations_7d_count > 0}
        onClick={onSelectExpiringThisWeek}
      />
      <KpiCard
        label="Needs review (past expiration, still open)"
        value={String(summary.needs_review_count)}
        attention={summary.needs_review_count > 0}
        onClick={onSelectNeedsReview}
      />
    </div>
  );
}
