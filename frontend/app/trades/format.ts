/** Display-only formatting helpers. Values stay strings end-to-end from the
 * API (see api-client.ts's Decimal convention) — these only touch them at
 * the last moment, for rendering, never for further calculation. */

export function formatMoney(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const num = Number(value);
  if (Number.isNaN(num)) return "—";
  return num.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function formatPercent(value: string | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "—";
  const num = Number(value);
  if (Number.isNaN(num)) return "—";
  return `${(num * 100).toFixed(digits)}%`;
}

const MONTHS = [
  "JAN",
  "FEB",
  "MAR",
  "APR",
  "MAY",
  "JUN",
  "JUL",
  "AUG",
  "SEP",
  "OCT",
  "NOV",
  "DEC",
];

/** Display-only: `DD MMM YY` (e.g. `04 SEP 24`). API/DB dates stay ISO
 * `YYYY-MM-DD` so filters, sorts, and Postgres DATE columns keep working. */
export function formatDateParts(
  value: string | null | undefined
): { day: string; month: string; year: string } | null {
  if (!value) return null;
  const isoDay = value.includes("T") ? value.slice(0, 10) : value;
  const [year, month, day] = isoDay.split("-");
  if (!year || !month || !day) return null;
  const monthIndex = Number(month) - 1;
  if (monthIndex < 0 || monthIndex > 11 || !Number.isFinite(monthIndex)) return null;
  return {
    day: day.padStart(2, "0"),
    month: MONTHS[monthIndex],
    year: year.slice(2),
  };
}

export function formatDate(value: string | null | undefined): string {
  const parts = formatDateParts(value);
  if (!parts) return value ? value : "—";
  return `${parts.day} ${parts.month} ${parts.year}`;
}

export function formatNumber(value: number | string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return String(value);
}

/** Share counts use a thousands separator (1,200 not 1200). */
export function formatShareCount(value: number | string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const num = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(num)) return "—";
  return num.toLocaleString();
}

/** Contracts × 100, or explicit `num_of_shares` for BTO/STC. */
export function tradeShareCount(trade: {
  num_of_contracts: number | null;
  num_of_shares: number | null;
}): number | null {
  if (trade.num_of_shares != null) return trade.num_of_shares;
  if (trade.num_of_contracts != null) return trade.num_of_contracts * 100;
  return null;
}

/** Display-only: net credit per share × shares (OKW NET CREDIT TOTAL). */
export function netCreditTotal(trade: {
  net_credit_per_share: string | null;
  num_of_contracts: number | null;
  num_of_shares: number | null;
}): string | null {
  const shares = tradeShareCount(trade);
  if (trade.net_credit_per_share == null || shares == null) return null;
  return String(Number(trade.net_credit_per_share) * shares);
}

/** Spreadsheet Amount: total dollars of the trade (premium × shares, or
 * stock price × shares). Options credits are the collected premium. */
export function tradeAmount(trade: {
  trade_type_category: string | null;
  is_credit: boolean;
  net_credit_per_share: string | null;
  credit_debit: string;
  price_per_share: string | null;
  num_of_contracts: number | null;
  num_of_shares: number | null;
}): number | null {
  const shares = tradeShareCount(trade);
  if (trade.net_credit_per_share != null && shares != null) {
    return Number(trade.net_credit_per_share) * shares;
  }
  if (trade.price_per_share != null && shares != null) {
    return Number(trade.price_per_share) * shares;
  }
  if (shares != null) {
    return Number(trade.credit_debit) * shares;
  }
  const fallback = Number(trade.credit_debit);
  return Number.isFinite(fallback) ? fallback : null;
}

export const DISPLAY_STATUS_LABEL: Record<string, string> = {
  open: "Open",
  rolled: "Rolled",
  assigned: "Assigned",
  expired: "Expired",
  closed: "Closed",
  paid: "Paid",
};

export const DISPLAY_STATUS_BADGE_CLASS: Record<string, string> = {
  open: "badgeOpen",
  rolled: "badgeRolled",
  assigned: "badgeAssigned",
  expired: "badgeExpired",
  closed: "badgeClosed",
  paid: "badgeOpen",
};

/** Stable per-type color so the same strategy always reads the same color
 * at a glance across the table — deliberately explicit rather than hashed,
 * so adding a new trade_type later means adding a line here, not hoping a
 * hash doesn't collide with an existing color. */
export const TRADE_TYPE_COLOR: Record<string, string> = {
  "ROCT PUT": "#4f46e5",
  "ROCT CALL": "#0ea5e9",
  "RULE ONE PUT": "#7c3aed",
  "RULE ONE CALL": "#14b8a6",
  "ROCS BULL PUT SPREAD": "#f59e0b",
  "BULL PUT SPREAD": "#ec4899",
  BTO: "#6b7280",
  STC: "#84cc16",
  DIVIDEND: "#059669",
};

export const LEDGER_SOURCE_LABEL: Record<string, string> = {
  option: "Option",
  BTO: "BTO",
  STC: "STC",
  dividend: "Dividend",
};
export const DEFAULT_TRADE_TYPE_COLOR = "#9a9a9e";
