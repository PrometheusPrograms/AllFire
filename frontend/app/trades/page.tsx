"use client";

import {
  addDays,
  endOfWeek,
  endOfYear,
  format,
  isAfter,
  startOfMonth,
  startOfWeek,
  startOfYear,
  sub,
  subYears,
} from "date-fns";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  getAnalyticsSummary,
  getPremiumTimeseries,
  listTrades,
  type AnalyticsSummary,
  type PremiumTimeseries,
  type Trade,
} from "@/lib/api-client";
import type { RangePreset } from "./DateRangeSelector";
import DetailDrawer from "./DetailDrawer";
import FilterBar, { EMPTY_FILTERS, type TradeFilters } from "./FilterBar";
import PremiumChart from "./PremiumChart";
import KpiStrip from "./KpiStrip";
import TickerDrawer from "./TickerDrawer";
import TradesTable, { PAGE_SIZE_OPTIONS, type PageSizeOption } from "./TradesTable";
import styles from "./trades.module.css";

const ACCOUNTS = ["All", "Rule 1", "Roth"] as const;
type AccountTab = (typeof ACCOUNTS)[number];

const DATE_FMT = "yyyy-MM-dd";
// Comfortably above the current ~730-trade dataset; used when the "All"
// page size is selected instead of a real pagination limit.
const ALL_ROWS_LIMIT = 5000;
// Safely before any real trade — used for the "All Time" date-range preset.
const ALL_TIME_START = "2000-01-01";

/** Friday of the current Mon-Fri week; if that's already passed (i.e. it's
 * the weekend), rolls forward to next week's Friday instead. */
function fridayOfThisWeek(now: Date): Date {
  const monday = startOfWeek(now, { weekStartsOn: 1 });
  let friday = addDays(monday, 4);
  if (isAfter(now, friday)) friday = addDays(friday, 7);
  return friday;
}

const RANGE_LABELS: Record<RangePreset, string> = {
  week: "This Week",
  month: "This Month",
  ytd: "YTD",
  lastYear: "Last Year",
  "12mo": "Last 12 Months",
  "5yr": "Last 5 Years",
  all: "All Time",
  custom: "Custom Range",
};

function rangeForPreset(preset: RangePreset, now: Date): { start: string; end: string } {
  switch (preset) {
    case "week": {
      const start = startOfWeek(now, { weekStartsOn: 1 });
      const end = endOfWeek(now, { weekStartsOn: 1 });
      return { start: format(start, DATE_FMT), end: format(end, DATE_FMT) };
    }
    case "month":
      return { start: format(startOfMonth(now), DATE_FMT), end: format(now, DATE_FMT) };
    case "lastYear": {
      const previous = subYears(now, 1);
      return {
        start: format(startOfYear(previous), DATE_FMT),
        end: format(endOfYear(previous), DATE_FMT),
      };
    }
    case "12mo":
      return { start: format(sub(now, { months: 12 }), DATE_FMT), end: format(now, DATE_FMT) };
    case "5yr":
      return { start: format(sub(now, { years: 5 }), DATE_FMT), end: format(now, DATE_FMT) };
    case "all":
      return { start: ALL_TIME_START, end: format(now, DATE_FMT) };
    case "ytd":
    default:
      return { start: format(startOfYear(now), DATE_FMT), end: format(now, DATE_FMT) };
  }
}

export default function TradesPage() {
  const [account, setAccount] = useState<AccountTab>("Rule 1");

  const [preset, setPreset] = useState<RangePreset>("ytd");
  const [customRange, setCustomRange] = useState(() => rangeForPreset("ytd", new Date()));
  const [bucket, setBucket] = useState<"day" | "week" | "month">("week");

  const range = useMemo(
    () => (preset === "custom" ? customRange : rangeForPreset(preset, new Date())),
    [preset, customRange]
  );

  const [filters, setFilters] = useState<TradeFilters>(EMPTY_FILTERS);
  const [pageSize, setPageSize] = useState<PageSizeOption>(PAGE_SIZE_OPTIONS[0]);
  const [offset, setOffset] = useState(0);

  const [summary, setSummary] = useState<AnalyticsSummary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(true);

  const [timeseries, setTimeseries] = useState<PremiumTimeseries | null>(null);
  const [chartLoading, setChartLoading] = useState(true);
  const [chartError, setChartError] = useState<string | null>(null);

  const [trades, setTrades] = useState<Trade[]>([]);
  const [tradesTotal, setTradesTotal] = useState(0);
  const [tradesLoading, setTradesLoading] = useState(true);
  const [tradesError, setTradesError] = useState<string | null>(null);

  const [selectedTradeId, setSelectedTradeId] = useState<number | null>(null);
  const [selectedTicker, setSelectedTicker] = useState<{
    ticker: string;
    accountName: string;
  } | null>(null);

  const accountParam = account === "All" ? undefined : account;
  const limit = pageSize === "all" ? ALL_ROWS_LIMIT : pageSize;

  const totalPremiumInRange = timeseries
    ? String(timeseries.items.reduce((sum, item) => sum + Number(item.premium_total), 0))
    : null;
  const totalPremiumRangeLabel = RANGE_LABELS[preset];

  useEffect(() => {
    setOffset(0);
  }, [account, filters, range.start, range.end, pageSize]);

  useEffect(() => {
    setSummaryLoading(true);
    getAnalyticsSummary({
      account: accountParam,
      date_from: range.start,
      date_to: range.end,
    })
      .then(setSummary)
      .catch(() => setSummary(null))
      .finally(() => setSummaryLoading(false));
  }, [accountParam, range.start, range.end]);

  useEffect(() => {
    setChartLoading(true);
    setChartError(null);
    getPremiumTimeseries({ account: accountParam, start: range.start, end: range.end, bucket })
      .then(setTimeseries)
      .catch((err) => setChartError(err instanceof Error ? err.message : "Failed to load chart."))
      .finally(() => setChartLoading(false));
  }, [accountParam, range.start, range.end, bucket]);

  const fetchTrades = useCallback(() => {
    setTradesLoading(true);
    setTradesError(null);

    if (filters.needsReviewOnly) {
      // Needs-review trades are irrespective of the selected date range
      // (they're flagged by expiration date vs. today, not date opened),
      // so skip the range/status filters entirely while this is active.
      listTrades({
        account: accountParam,
        ticker: filters.ticker || undefined,
        trade_type: filters.tradeType || undefined,
        needs_review_computed: true,
        limit,
        offset,
      })
        .then((response) => {
          setTrades(response.items);
          setTradesTotal(response.total);
        })
        .catch((err) =>
          setTradesError(err instanceof Error ? err.message : "Failed to load trades.")
        )
        .finally(() => setTradesLoading(false));
      return;
    }

    if (filters.expiringThisWeekOnly) {
      // Expiring-this-week is also independent of the chart's date range —
      // it filters on expiration_date, not date_trade_open.
      const today = new Date();
      listTrades({
        account: accountParam,
        ticker: filters.ticker || undefined,
        trade_type: filters.tradeType || undefined,
        status: "open",
        expiration_from: format(today, DATE_FMT),
        expiration_to: format(fridayOfThisWeek(today), DATE_FMT),
        limit,
        offset,
      })
        .then((response) => {
          setTrades(response.items);
          setTradesTotal(response.total);
        })
        .catch((err) =>
          setTradesError(err instanceof Error ? err.message : "Failed to load trades.")
        )
        .finally(() => setTradesLoading(false));
      return;
    }

    // Server-side display_status filtering only supports one value at a
    // time; when multiple statuses are selected we fetch unfiltered by
    // status and filter client-side on this page (a documented tradeoff,
    // not a bug — see FilterBar's statuses prop).
    const singleStatus = filters.statuses.length === 1 ? filters.statuses[0] : undefined;
    listTrades({
      account: accountParam,
      ticker: filters.ticker || undefined,
      trade_type: filters.tradeType || undefined,
      display_status: singleStatus,
      date_from: range.start,
      date_to: range.end,
      limit,
      offset,
    })
      .then((response) => {
        if (filters.statuses.length > 1) {
          const filtered = response.items.filter(
            (t) => t.display_status != null && filters.statuses.includes(t.display_status)
          );
          setTrades(filtered);
          setTradesTotal(filtered.length);
        } else {
          setTrades(response.items);
          setTradesTotal(response.total);
        }
      })
      .catch((err) => setTradesError(err instanceof Error ? err.message : "Failed to load trades."))
      .finally(() => setTradesLoading(false));
  }, [accountParam, filters, range.start, range.end, limit, offset]);

  useEffect(() => {
    fetchTrades();
  }, [fetchTrades]);

  return (
    <div className={styles.page}>
      <div className={styles.container}>
        <div className={styles.header}>
          <h1 className={styles.title}>Trades</h1>
          <div className={styles.tabs}>
            {ACCOUNTS.map((a) => (
              <button
                key={a}
                type="button"
                className={account === a ? `${styles.tab} ${styles.tabActive}` : styles.tab}
                onClick={() => setAccount(a)}
              >
                {a}
              </button>
            ))}
          </div>
        </div>

        <KpiStrip
          summary={summary}
          loading={summaryLoading}
          onSelectNeedsReview={() =>
            setFilters((f) => ({ ...f, needsReviewOnly: true, expiringThisWeekOnly: false }))
          }
          onSelectExpiringThisWeek={() =>
            setFilters((f) => ({ ...f, expiringThisWeekOnly: true, needsReviewOnly: false }))
          }
          totalPremiumInRange={totalPremiumInRange}
          totalPremiumRangeLabel={totalPremiumRangeLabel}
        />

        <PremiumChart
          data={timeseries}
          loading={chartLoading}
          error={chartError}
          preset={preset}
          onPresetChange={setPreset}
          start={customRange.start}
          end={customRange.end}
          onCustomRangeChange={(start, end) => {
            setCustomRange({ start, end });
            setPreset("custom");
          }}
          bucket={bucket}
          onBucketChange={setBucket}
          onBarClick={(periodStart, periodEnd) => {
            setCustomRange({ start: periodStart, end: periodEnd });
            setPreset("custom");
          }}
        />

        <FilterBar filters={filters} onChange={setFilters} />

        <TradesTable
          trades={trades}
          loading={tradesLoading}
          error={tradesError}
          total={tradesTotal}
          limit={limit}
          offset={offset}
          onOffsetChange={setOffset}
          pageSize={pageSize}
          onPageSizeChange={setPageSize}
          onSelectTrade={setSelectedTradeId}
          onSelectTicker={(ticker, accountName) =>
            setSelectedTicker({ ticker, accountName })
          }
        />
      </div>

      {selectedTicker !== null && (
        <TickerDrawer
          ticker={selectedTicker.ticker}
          accountName={selectedTicker.accountName}
          onClose={() => setSelectedTicker(null)}
          onSelectTrade={setSelectedTradeId}
        />
      )}

      {selectedTradeId !== null && (
        <DetailDrawer tradeId={selectedTradeId} onClose={() => setSelectedTradeId(null)} />
      )}
    </div>
  );
}
