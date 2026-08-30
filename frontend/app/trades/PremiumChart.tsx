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
  const items = data?.items ?? [];
  const chartData = items.map((item) => ({
    label: formatDate(item.period_start),
    premium: Number(item.premium_total),
    trade_count: item.trade_count,
    period_start: item.period_start,
    period_end: item.period_end,
  }));

  const totalPremium = chartData.reduce((sum, d) => sum + d.premium, 0);
  // Only average over buckets that actually had trades — a bucket with no
  // trades (missing/pre-tracking data) shouldn't drag the average down as
  // if it were a genuine zero-premium period.
  const bucketsWithTrades = chartData.filter((d) => d.trade_count > 0);
  const averagePerBucket =
    bucketsWithTrades.length > 0
      ? bucketsWithTrades.reduce((sum, d) => sum + d.premium, 0) / bucketsWithTrades.length
      : 0;

  return (
    <div className={styles.card}>
      <div className={styles.cardHeader}>
        <div>
          <span className={styles.cardTitle}>Premium over time</span>
          <span className={styles.cardTitleTotal}> — click a bar to filter trades to that period</span>
        </div>
        <div className={styles.presetGroup}>
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

      {error && <div className={styles.error}>{error}</div>}
      {loading && !error && <div className={styles.loading}>Loading chart…</div>}
      {!loading && !error && chartData.length === 0 && (
        <div className={styles.emptyState}>No premium data in this range.</div>
      )}
      {!loading && !error && chartData.length > 0 && (
        <ResponsiveContainer width="100%" height={260}>
          <BarChart data={chartData} margin={{ top: 24, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" opacity={0.2} vertical={false} />
            <XAxis dataKey="label" fontSize={11} tickMargin={8} />
            <YAxis
              fontSize={11}
              width={70}
              tickFormatter={(v) => `$${Number(v).toLocaleString()}`}
            />
            <Tooltip formatter={(value) => formatMoney(String(value))} />
            <Bar
              dataKey="premium"
              fill="#4f46e5"
              radius={[4, 4, 0, 0]}
              cursor="pointer"
              onClick={(barData: { payload?: { period_start: string; period_end: string } }) => {
                if (barData.payload) onBarClick(barData.payload.period_start, barData.payload.period_end);
              }}
            />
            <ReferenceLine y={averagePerBucket} stroke="#f5811e" strokeDasharray="4 4">
              <Label
                value={`Avg: ${formatMoney(String(averagePerBucket))}`}
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
