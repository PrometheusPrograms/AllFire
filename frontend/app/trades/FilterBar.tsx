import type { DisplayStatus } from "@/lib/api-client";
import { DISPLAY_STATUS_LABEL } from "./format";
import styles from "./trades.module.css";

const ALL_STATUSES: DisplayStatus[] = ["open", "rolled", "assigned", "expired", "closed"];

export interface TradeFilters {
  ticker: string;
  tradeType: string;
  statuses: DisplayStatus[];
  needsReviewOnly: boolean;
  expiringThisWeekOnly: boolean;
}

export const EMPTY_FILTERS: TradeFilters = {
  ticker: "",
  tradeType: "",
  statuses: [],
  needsReviewOnly: false,
  expiringThisWeekOnly: false,
};

const TRADE_TYPES = [
  "ROCT PUT",
  "RULE ONE PUT",
  "ROCT CALL",
  "RULE ONE CALL",
  "ROCS BULL PUT SPREAD",
  "BULL PUT SPREAD",
  "BTO",
  "STC",
];

export default function FilterBar({
  filters,
  onChange,
}: {
  filters: TradeFilters;
  onChange: (filters: TradeFilters) => void;
}) {
  function toggleStatus(status: DisplayStatus) {
    const statuses = filters.statuses.includes(status)
      ? filters.statuses.filter((s) => s !== status)
      : [...filters.statuses, status];
    onChange({ ...filters, statuses });
  }

  const hasActiveFilters =
    filters.ticker !== "" ||
    filters.tradeType !== "" ||
    filters.statuses.length > 0 ||
    filters.needsReviewOnly ||
    filters.expiringThisWeekOnly;

  return (
    <div className={styles.filterBar}>
      {filters.needsReviewOnly && (
        <button
          type="button"
          className={`${styles.chip} ${styles.chipActive}`}
          onClick={() => onChange({ ...filters, needsReviewOnly: false })}
        >
          Needs review ✕
        </button>
      )}
      {filters.expiringThisWeekOnly && (
        <button
          type="button"
          className={`${styles.chip} ${styles.chipActive}`}
          onClick={() => onChange({ ...filters, expiringThisWeekOnly: false })}
        >
          Expiring this week ✕
        </button>
      )}
      <input
        type="text"
        className={styles.searchInput}
        placeholder="Ticker (e.g. ADBE)"
        value={filters.ticker}
        onChange={(e) => onChange({ ...filters, ticker: e.target.value.toUpperCase() })}
      />
      <select
        className={styles.select}
        value={filters.tradeType}
        onChange={(e) => onChange({ ...filters, tradeType: e.target.value })}
      >
        <option value="">All trade types</option>
        {TRADE_TYPES.map((t) => (
          <option key={t} value={t}>
            {t}
          </option>
        ))}
      </select>
      <div className={styles.statusChips}>
        {ALL_STATUSES.map((status) => (
          <button
            key={status}
            type="button"
            className={
              filters.statuses.includes(status)
                ? `${styles.chip} ${styles.chipActive}`
                : styles.chip
            }
            onClick={() => toggleStatus(status)}
          >
            {DISPLAY_STATUS_LABEL[status]}
          </button>
        ))}
      </div>
      {hasActiveFilters && (
        <button
          type="button"
          className={styles.clearButton}
          onClick={() => onChange(EMPTY_FILTERS)}
        >
          Clear filters
        </button>
      )}
    </div>
  );
}
