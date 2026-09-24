/**
 * Thin wrapper around the backend API base URL, plus typed helpers for the
 * calculator endpoints exposed at `/api/calculators/*`.
 *
 * All financial fields are passed and returned as strings — the backend
 * parses/serializes them as `Decimal`, so we avoid ever routing these
 * values through JS `number`.
 */

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

async function postJson<TResponse>(
  path: string,
  body: Record<string, unknown>
): Promise<TResponse> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${detail}`);
  }

  return response.json() as Promise<TResponse>;
}

/** Drops null/undefined params and stringifies the rest, so callers can pass
 * optional filters straight through without each call site filtering them. */
function toQueryString(
  params: Record<string, string | number | boolean | undefined | null>
): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

async function getJson<TResponse>(
  path: string,
  params: Record<string, string | number | boolean | undefined | null> = {}
): Promise<TResponse> {
  const response = await fetch(`${API_BASE_URL}${path}${toQueryString(params)}`);

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${detail}`);
  }

  return response.json() as Promise<TResponse>;
}

export interface RorcRequest {
  net_credit_per_share: string;
  risk_capital_per_share: string;
  margin_percent?: string;
}

export interface RorcResponse {
  rorc: string;
}

export function calculateRorc(payload: RorcRequest): Promise<RorcResponse> {
  return postJson<RorcResponse>("/api/calculators/rorc", { ...payload });
}

export interface ArorcRequest {
  rorc: string;
  days_to_expiration: number;
}

export interface ArorcResponse {
  arorc: string;
}

export function calculateArorc(payload: ArorcRequest): Promise<ArorcResponse> {
  return postJson<ArorcResponse>("/api/calculators/arorc", { ...payload });
}

export interface KellyRequest {
  probability_of_winning: string;
  avg_win: string;
  avg_loss: string;
}

export interface KellyResponse {
  kelly: string;
}

export function calculateKelly(payload: KellyRequest): Promise<KellyResponse> {
  return postJson<KellyResponse>("/api/calculators/kelly", { ...payload });
}

export interface CostBasisRequest {
  prior_running_basis: string;
  prior_running_shares: string;
  total_amount: string;
  shares: string;
}

export interface CostBasisResponse {
  running_basis: string;
  running_shares: string;
  basis_per_share: string | null;
}

export function calculateCostBasis(
  payload: CostBasisRequest
): Promise<CostBasisResponse> {
  return postJson<CostBasisResponse>("/api/calculators/cost-basis", {
    ...payload,
  });
}

// ---------------------------------------------------------------------------
// Trades — /api/trades/*
// ---------------------------------------------------------------------------

export type DisplayStatus = "open" | "rolled" | "assigned" | "expired" | "closed";

export interface TradeEvent {
  id: number;
  event_type: string;
  event_date: string;
  closing_debit: string | null;
  total_debit: string | null;
  schwab_order_id: string | null;
  notes: string | null;
}

export interface ChainEvent extends TradeEvent {
  trade_id: number;
  strike_price: string | null;
  expiration_date: string | null;
  date_trade_open: string | null;
  net_credit_per_share: string | null;
  arorc: string | null;
  next_trade_id: number | null;
  roll_kind: string | null;
  option_right: string | null;
  to_strike: string | null;
  to_expiration: string | null;
}

export interface Trade {
  id: number;
  account_id: number;
  account_name: string;
  ticker: string;
  trade_type: string;
  trade_type_category: string | null;
  is_credit: boolean;
  date_trade_open: string;
  expiration_date: string | null;
  days_to_expiration: number | null;
  num_of_contracts: number | null;
  num_of_shares: number | null;
  strike_price: string | null;
  long_strike: string | null;
  price_per_share: string | null;
  credit_debit: string;
  commission_per_share: string;
  net_credit_per_share: string | null;
  risk_capital_per_share: string | null;
  margin_percent: string | null;
  arorc: string | null;
  needs_review: boolean;
  trade_status: "open" | "closed";
  display_status: DisplayStatus | null;
  date_trade_closed: string | null;
  date_trade_rolled: string | null;
  trade_parent_id: number | null;
  roll_legs: number;
  strike_path: string | null;
  chain_trade_ids: number[];
  premium_collected: string | null;
  chain_arorc: string | null;
  chain_net_credit_per_share: string | null;
  chain_dte: number | null;
  closing_debit: string | null;
  total_debit: string | null;
  result: string | null;
  result_date: string | null;
  result_net_credit: string | null;
  delta: string | null;
  probability_of_winning: string | null;
  final_arorc: string | null;
}

export interface TradeDetail extends Trade {
  events: TradeEvent[];
  running_basis: string | null;
  running_shares: string | null;
  chain_root_id: number;
  chain_events: ChainEvent[];
  chain_legs: Trade[];
}

export interface TradeListResponse {
  total: number;
  items: Trade[];
}

export interface ListTradesParams {
  account?: string;
  ticker?: string;
  trade_type?: string;
  status?: "open" | "closed";
  display_status?: DisplayStatus;
  needs_review?: boolean;
  needs_review_computed?: boolean;
  as_of?: string;
  date_from?: string;
  date_to?: string;
  expiration_from?: string;
  expiration_to?: string;
  limit?: number;
  offset?: number;
}

export function listTrades(params: ListTradesParams = {}): Promise<TradeListResponse> {
  return getJson<TradeListResponse>("/api/trades", { ...params });
}

export function getTrade(id: number): Promise<TradeDetail> {
  return getJson<TradeDetail>(`/api/trades/${id}`);
}

// ---------------------------------------------------------------------------
// Analytics — /api/analytics/*
// ---------------------------------------------------------------------------

export interface UpcomingExpiration {
  id: number;
  ticker: string;
  trade_type: string;
  expiration_date: string;
}

export interface AnalyticsSummary {
  as_of: string;
  open_trades_count: number;
  avg_arorc_open: string | null;
  upcoming_expirations_7d_count: number;
  upcoming_expirations_7d: UpcomingExpiration[];
  needs_review_count: number;
  premium_this_week: string;
  avg_weekly_premium_ytd: string;
  total_premium_ytd: string;
}

export interface GetAnalyticsSummaryParams {
  account?: string;
  as_of?: string;
  date_from?: string;
  date_to?: string;
}

export function getAnalyticsSummary(
  params: GetAnalyticsSummaryParams = {}
): Promise<AnalyticsSummary> {
  return getJson<AnalyticsSummary>("/api/analytics/summary", { ...params });
}

export interface PremiumPeriod {
  period_start: string;
  period_end: string;
  premium_total: string;
  trade_count: number;
}

export interface PremiumTimeseries {
  bucket: "day" | "week" | "month";
  items: PremiumPeriod[];
}

export interface GetPremiumTimeseriesParams {
  start: string;
  end: string;
  account?: string;
  bucket?: "day" | "week" | "month";
}

export function getPremiumTimeseries(
  params: GetPremiumTimeseriesParams
): Promise<PremiumTimeseries> {
  return getJson<PremiumTimeseries>("/api/analytics/premium-timeseries", {
    ...params,
  });
}

// ---------------------------------------------------------------------------
// Positions — /api/positions/* (the ticker drill-down)
// ---------------------------------------------------------------------------

export interface ClassTotals {
  shares: string;
  cost_basis: string;
  avg_cost_per_share: string | null;
}

export interface PositionSummary {
  ticker: string;
  account_name: string;
  total: ClassTotals;
  trading: ClassTotals;
  long_term: ClassTotals;
  total_premium_collected: string;
}

export function getPositionSummary(params: {
  ticker: string;
  account: string;
}): Promise<PositionSummary> {
  return getJson<PositionSummary>("/api/positions/summary", { ...params });
}

export type LedgerSource = "option" | "BTO" | "STC" | "dividend";
export type LedgerRowKind = "trade" | "dividend";

export interface PositionLedgerRow {
  row_kind: LedgerRowKind;
  source: LedgerSource;
  id: number;
  date: string;
  trade_type: string;
  strike_price: string | null;
  long_strike: string | null;
  expiration_date: string | null;
  shares: number | null;
  amount: string | null;
  // null for BTO/STC — status is an options-lifecycle concept and doesn't
  // apply to a plain stock fill.
  display_status: string | null;
}

export interface PositionLedger {
  ticker: string;
  account_name: string;
  items: PositionLedgerRow[];
}

export function getPositionLedger(params: {
  ticker: string;
  account: string;
}): Promise<PositionLedger> {
  return getJson<PositionLedger>("/api/positions/ledger", { ...params });
}
