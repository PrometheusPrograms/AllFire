import { useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Label,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { PremiumTimeseries } from "@/lib/api-client";
import DateRangeSelector, { type RangePreset } from "./DateRangeSelector";
import { formatDate, formatMoney } from "./format";
import styles from "./trades.module.css";

export type { RangePreset };

/** Which series the chart plots. Both live in the same timeseries payload
 * (premium sum + trade count per bucket), so switching is a client-side
 * toggle with no refetch. "trades" counts the same premium-generating
 * trades the premium series sums — the two charts always agree on which
 * rows exist (see backend `bucket_premium`). */
type Metric = "premium" | "trades";

// Distinct hues so the metric switch is legible at a glance: indigo for
// money, teal for counts. The average line stays orange for both.
const BAR_COLOR: Record<Metric, string> = {
  premium: "#4f46e5",
  trades: "#0d9488",
};

const BUCKET_NOUN: Record<"day" | "week" | "month", string> = {
  day: "day",
  week: "week",
  month: "month",
};

export default function PremiumChart({
  data,
  loading,
  error,
  preset,
  onPresetChange,
  start,
  end,
  onCustomRangeChange,
  bucket,
  onBucketChange,
  onBarClick,
}: {
  data: PremiumTimeseries | null;
  loading: boolean;
  error: string | null;
  preset: RangePreset;
  onPresetChange: (preset: RangePreset) => void;
  start: string;
  end: string;
  onCustomRangeChange: (start: string, end: string) => void;
  bucket: "day" | "week" | "month";
  onBucketChange: (bucket: "day" | "week" | "month") => void;
  onBarClick: (periodStart: string, periodEnd: string) => void;
}) {
  const [metric, setMetric] = useState<Metric>("premium");

  const items = data?.items ?? [];
  const chartData = items.map((item) => ({
    label: formatDate(item.period_start),
    premium: Number(item.premium_total),
    trade_count: item.trade_count,
    period_start: item.period_start,
    period_end: item.period_end,
  }));

  const isPremium = metric === "premium";
  const dataKey = isPremium ? "premium" : "trade_count";

  // Only average over buckets that actually had trades — a bucket with no
  // trades (missing/pre-tracking data) shouldn't drag the average down as
  // if it were a genuine zero period. Same rule for both metrics so the
  // two averages stay comparable.
  const bucketsWithTrades = chartData.filter((d) => d.trade_count > 0);
  const averagePerBucket =
    bucketsWithTrades.length > 0
      ? bucketsWithTrades.reduce(
          (sum, d) => sum + (isPremium ? d.premium : d.trade_count),
          0
        ) / bucketsWithTrades.length
      : 0;

  const bucketNoun = BUCKET_NOUN[bucket];
  const averageLabel = isPremium
    ? `Avg: ${formatMoney(String(averagePerBucket))}`
    : `Avg: ${averagePerBucket.toFixed(1)} / ${bucketNoun}`;

  return (
    <div className={styles.card}>
      <div className={styles.cardHeader}>
        <div className={styles.cardHeaderTitle}>
          <span className={styles.cardTitle}>
            {isPremium ? "Premium over time" : "Trades over time"}
          </span>
          <span className={styles.cardTitleTotal}> — click a bar to filter trades to that period</span>
        </div>
        <div className={styles.chartControls}>
          <div className={styles.metricToggle}>
            <button
              type="button"
              className={
                isPremium
                  ? `${styles.presetButton} ${styles.presetButtonActive}`
                  : styles.presetButton
              }
              onClick={() => setMetric("premium")}
            >
              Premium
            </button>
            <button
              type="button"
              className={
                !isPremium
                  ? `${styles.presetButton} ${styles.presetButtonActive}`
                  : styles.presetButton
              }
              onClick={() => setMetric("trades")}
            >
              Trades
            </button>
          </div>
          <div className={styles.cardHeaderRight}>
            <DateRangeSelector
              preset={preset}
              onPresetChange={onPresetChange}
              start={start}
              end={end}
              onCustomRangeChange={onCustomRangeChange}
            />
            <div className={styles.bucketToggle}>
              <button
                type="button"
                className={
                  bucket === "day"
                    ? `${styles.presetButton} ${styles.presetButtonActive}`
                    : styles.presetButton
                }
                onClick={() => onBucketChange("day")}
              >
                Daily
              </button>
              <button
                type="button"
                className={
                  bucket === "week"
                    ? `${styles.presetButton} ${styles.presetButtonActive}`
                    : styles.presetButton
                }
                onClick={() => onBucketChange("week")}
              >
                Weekly
              </button>
              <button
                type="button"
                className={
                  bucket === "month"
                    ? `${styles.presetButton} ${styles.presetButtonActive}`
                    : styles.presetButton
                }
                onClick={() => onBucketChange("month")}
              >
                Monthly
              </button>
            </div>
          </div>
        </div>
      </div>

      {error && <div className={styles.error}>{error}</div>}
      {loading && !error && <div className={styles.loading}>Loading chart…</div>}
      {!loading && !error && chartData.length === 0 && (
        <div className={styles.emptyState}>
          {isPremium ? "No premium data in this range." : "No trades in this range."}
        </div>
      )}
      {!loading && !error && chartData.length > 0 && (
        <ResponsiveContainer width="100%" height={260}>
          <BarChart data={chartData} margin={{ top: 24, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" opacity={0.2} vertical={false} />
            <XAxis dataKey="label" fontSize={11} tickMargin={8} />
            <YAxis
              fontSize={11}
              width={70}
              allowDecimals={isPremium}
              tickFormatter={(v) =>
                isPremium ? `$${Number(v).toLocaleString()}` : String(v)
              }
            />
            <Tooltip
              formatter={(value) =>
                isPremium
                  ? formatMoney(String(value))
                  : `${value} trade${Number(value) === 1 ? "" : "s"}`
              }
            />
            <Bar
              dataKey={dataKey}
              fill={BAR_COLOR[metric]}
              radius={[4, 4, 0, 0]}
              cursor="pointer"
              onClick={(barData: { payload?: { period_start: string; period_end: string } }) => {
                if (barData.payload) onBarClick(barData.payload.period_start, barData.payload.period_end);
              }}
            />
            <ReferenceLine y={averagePerBucket} stroke="#f5811e" strokeDasharray="4 4">
              <Label
                value={averageLabel}
                position="insideTopRight"
                dy={-14}
                fill="#f5811e"
                fontSize={11}
                fontWeight={700}
              />
            </ReferenceLine>
          </BarChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
