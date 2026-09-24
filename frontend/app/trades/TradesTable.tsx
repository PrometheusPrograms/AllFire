import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type SortingState,
} from "@tanstack/react-table";
import { useState } from "react";
import type { Trade } from "@/lib/api-client";
import FormattedDate from "./FormattedDate";
import {
  DEFAULT_TRADE_TYPE_COLOR,
  DISPLAY_STATUS_BADGE_CLASS,
  DISPLAY_STATUS_LABEL,
  formatMoney,
  formatPercent,
  formatShareCount,
  tradeAmount,
  tradeShareCount,
  TRADE_TYPE_COLOR,
} from "./format";
import styles from "./trades.module.css";

export const PAGE_SIZE_OPTIONS = [100, 250, 500, "all"] as const;
export type PageSizeOption = (typeof PAGE_SIZE_OPTIONS)[number];

const MAX_LEADING_PAGES = 10;

/** First 10 pages, then "…", then the last page — e.g. for 25 pages:
 * [1, 2, ..., 10, "…", 25]. Simple and predictable rather than windowed
 * around the current page, per product decision. */
function buildPageNumbers(total: number): (number | "…")[] {
  if (total <= MAX_LEADING_PAGES) {
    return Array.from({ length: total }, (_, i) => i + 1);
  }
  const leading = Array.from({ length: MAX_LEADING_PAGES }, (_, i) => i + 1);
  return [...leading, "…", total];
}

const columnHelper = createColumnHelper<Trade>();

// Right-aligned so decimal points line up down the column, per user request.
const NUMERIC_COLUMNS = new Set(["arorc", "total_premium", "amount", "size"]);

interface TradesTableMeta {
  onSelectTicker: (ticker: string, accountName: string) => void;
}

const columns = [
  columnHelper.accessor("date_trade_open", {
    header: "Opened",
    cell: (info) => <FormattedDate value={info.getValue()} />,
  }),
  columnHelper.accessor("ticker", {
    header: "Ticker",
    cell: (info) => {
      const meta = info.table.options.meta as TradesTableMeta;
      const trade = info.row.original;
      return (
        <button
          type="button"
          className={styles.tickerLink}
          onClick={(e) => {
            // Independent from the row click (which opens the single-trade
            // drawer) — this opens the lifetime ticker/account position.
            e.stopPropagation();
            meta?.onSelectTicker(trade.ticker, trade.account_name);
          }}
        >
          {info.getValue()}
          {trade.roll_legs > 1 ? (
            <span className={styles.rollLegs}>{trade.roll_legs} legs</span>
          ) : null}
        </button>
      );
    },
  }),
  columnHelper.accessor("trade_type", {
    header: "Type",
    cell: (info) => {
      const type = info.getValue();
      return (
        <span className={styles.typeCell}>
          <span
            className={styles.typeDot}
            style={{ backgroundColor: TRADE_TYPE_COLOR[type] ?? DEFAULT_TRADE_TYPE_COLOR }}
          />
          {type}
        </span>
      );
    },
  }),
  columnHelper.accessor(
    (row) => {
      if (row.strike_path) {
        return row.strike_path
          .split(" → ")
          .map((part) => formatMoney(part))
          .join(" → ");
      }
      return row.long_strike
        ? `${formatMoney(row.strike_price)}/${formatMoney(row.long_strike)}`
        : row.strike_price
          ? formatMoney(row.strike_price)
          : null;
    },
    {
      id: "strike",
      header: "Strike(s)",
      cell: (info) => info.getValue() ?? "—",
    }
  ),
  columnHelper.accessor("expiration_date", {
    header: "Expiration",
    cell: (info) => <FormattedDate value={info.getValue()} />,
  }),
  columnHelper.accessor((row) => tradeShareCount(row), {
    id: "size",
    header: "Shares",
    cell: (info) => formatShareCount(info.getValue()),
  }),
  columnHelper.accessor((row) => tradeAmount(row), {
    id: "amount",
    header: "Amount",
    cell: (info) => {
      const value = info.getValue();
      return value === null ? "—" : formatMoney(String(value));
    },
  }),
  columnHelper.accessor(
    (row) => {
      if (row.premium_collected != null) return Number(row.premium_collected);
      if (row.trade_type_category !== "OPTIONS" || !row.is_credit) return null;
      if (row.net_credit_per_share === null || row.num_of_contracts === null) return null;
      return Number(row.net_credit_per_share) * row.num_of_contracts * 100;
    },
    {
      id: "total_premium",
      header: "Total Premium",
      cell: (info) => {
        const value = info.getValue();
        return value === null ? "—" : formatMoney(String(value));
      },
    }
  ),
  columnHelper.accessor((row) => row.chain_arorc ?? row.arorc, {
    id: "arorc",
    header: "ARORC",
    cell: (info) => formatPercent(info.getValue()),
  }),
  columnHelper.accessor("display_status", {
    header: "Status",
    cell: (info) => {
      const status = info.getValue();
      if (status == null) return "—";
      return (
        <span className={`${styles.badge} ${styles[DISPLAY_STATUS_BADGE_CLASS[status]]}`}>
          {DISPLAY_STATUS_LABEL[status]}
        </span>
      );
    },
  }),
];

export default function TradesTable({
  trades,
  loading,
  error,
  total,
  limit,
  offset,
  onOffsetChange,
  pageSize,
  onPageSizeChange,
  onSelectTrade,
  onSelectTicker,
}: {
  trades: Trade[];
  loading: boolean;
  error: string | null;
  total: number;
  limit: number;
  offset: number;
  onOffsetChange: (offset: number) => void;
  pageSize: PageSizeOption;
  onPageSizeChange: (pageSize: PageSizeOption) => void;
  onSelectTrade: (id: number) => void;
  onSelectTicker: (ticker: string, accountName: string) => void;
}) {
  const [sorting, setSorting] = useState<SortingState>([]);

  const table = useReactTable({
    data: trades,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    meta: { onSelectTicker } satisfies TradesTableMeta,
  });

  const pageStart = offset + 1;
  const pageEnd = Math.min(offset + limit, total);
  const totalPages = Math.max(1, Math.ceil(total / limit));
  const currentPage = Math.floor(offset / limit) + 1;
  const pageNumbers = buildPageNumbers(totalPages);

  return (
    <div className={styles.card}>
      <div className={styles.cardHeader}>
        <span className={styles.cardTitle}>Trades ({total})</span>
        <label className={styles.pageSizeLabel}>
          Show
          <select
            className={styles.select}
            value={String(pageSize)}
            onChange={(e) =>
              onPageSizeChange(
                e.target.value === "all" ? "all" : (Number(e.target.value) as PageSizeOption)
              )
            }
          >
            {PAGE_SIZE_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {option === "all" ? "All" : option}
              </option>
            ))}
          </select>
          rows
        </label>
      </div>

      {error && <div className={styles.error}>{error}</div>}

      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map((header) => (
                  <th
                    key={header.id}
                    className={NUMERIC_COLUMNS.has(header.column.id) ? styles.numeric : undefined}
                    onClick={header.column.getToggleSortingHandler()}
                  >
                    {flexRender(header.column.columnDef.header, header.getContext())}
                    {{ asc: " ▲", desc: " ▼" }[header.column.getIsSorted() as string] ?? ""}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {!loading &&
              table.getRowModel().rows.map((row) => (
                <tr key={row.id} onClick={() => onSelectTrade(row.original.id)}>
                  {row.getVisibleCells().map((cell) => (
                    <td
                      key={cell.id}
                      className={NUMERIC_COLUMNS.has(cell.column.id) ? styles.numeric : undefined}
                    >
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  ))}
                </tr>
              ))}
          </tbody>
        </table>
        {loading && <div className={styles.loading}>Loading trades…</div>}
        {!loading && !error && trades.length === 0 && (
          <div className={styles.emptyState}>No trades match these filters.</div>
        )}
      </div>

      {total > 0 && (
        <div className={styles.pagination}>
          <span>
            {pageStart}–{pageEnd} of {total}
          </span>
          {limit < total && (
            <div className={styles.pageButtons}>
              <button
                type="button"
                className={styles.pageButton}
                disabled={offset === 0}
                onClick={() => onOffsetChange(Math.max(0, offset - limit))}
              >
                Previous
              </button>
              {pageNumbers.map((p, i) =>
                p === "…" ? (
                  <span key={`ellipsis-${i}`} className={styles.pageEllipsis}>
                    …
                  </span>
                ) : (
                  <button
                    key={p}
                    type="button"
                    className={
                      p === currentPage
                        ? `${styles.pageButton} ${styles.pageButtonActive}`
                        : styles.pageButton
                    }
                    onClick={() => onOffsetChange((p - 1) * limit)}
                  >
                    {p}
                  </button>
                )
              )}
              <button
                type="button"
                className={styles.pageButton}
                disabled={offset + limit >= total}
                onClick={() => onOffsetChange(offset + limit)}
              >
                Next
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
