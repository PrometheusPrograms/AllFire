import styles from "./trades.module.css";

export type RangePreset =
  | "week"
  | "month"
  | "ytd"
  | "lastYear"
  | "12mo"
  | "5yr"
  | "all"
  | "custom";

export const RANGE_PRESETS: { id: RangePreset; label: string }[] = [
  { id: "week", label: "This Week" },
  { id: "month", label: "This Month" },
  { id: "ytd", label: "YTD" },
  { id: "lastYear", label: "Last Year" },
  { id: "12mo", label: "Last 12 Months" },
  { id: "5yr", label: "Last 5 Years" },
  { id: "all", label: "All Time" },
  { id: "custom", label: "Custom" },
];

/** Shared date-range control — used both above the premium chart and next
 * to the trades filters, since both views read from the same underlying
 * `preset`/`customRange` state (see page.tsx). */
export default function DateRangeSelector({
  preset,
  onPresetChange,
  start,
  end,
  onCustomRangeChange,
}: {
  preset: RangePreset;
  onPresetChange: (preset: RangePreset) => void;
  start: string;
  end: string;
  onCustomRangeChange: (start: string, end: string) => void;
}) {
  return (
    <div className={styles.presetGroup}>
      {RANGE_PRESETS.map((p) => (
        <button
          key={p.id}
          type="button"
          className={
            preset === p.id
              ? `${styles.presetButton} ${styles.presetButtonActive}`
              : styles.presetButton
          }
          onClick={() => onPresetChange(p.id)}
        >
          {p.label}
        </button>
      ))}
      {preset === "custom" && (
        <>
          <input
            type="date"
            className={styles.dateInput}
            value={start}
            onChange={(e) => onCustomRangeChange(e.target.value, end)}
          />
          <span>–</span>
          <input
            type="date"
            className={styles.dateInput}
            value={end}
            onChange={(e) => onCustomRangeChange(start, e.target.value)}
          />
        </>
      )}
    </div>
  );
}
