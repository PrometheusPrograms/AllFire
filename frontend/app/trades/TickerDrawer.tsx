import { useEffect, useMemo, useState } from "react";
import {
  getPositionSummary,
  listTrades,
  type PositionSummary,
  type Trade,
} from "@/lib/api-client";
import {
  DEFAULT_TRADE_TYPE_COLOR,
  DISPLAY_STATUS_BADGE_CLASS,
  DISPLAY_STATUS_LABEL,
  formatDate,
  formatMoney,
  formatShareCount,
  tradeAmount,
  tradeShareCount,
  TRADE_TYPE_COLOR,
} from "./format";
import styles from "./trades.module.css";

const ALL_TRADES_LIMIT = 2000;

type ShareClass = "trading" | "long_term";
type NotionalKind = "SHARES" | "SELL_PUT" | "SELL_CALL";

interface NotionalTrade {
  id: string;
  kind: NotionalKind;
  shareClass: ShareClass;
  quantity: number;
  price: number;
  premiumPerShare: number;
  assigned: boolean;
  openedDate: string;
}

const NOTIONAL_KIND_LABEL: Record<NotionalKind, string> = {
  SHARES: "Buy / sell shares",
  SELL_PUT: "Sell put",
  SELL_CALL: "Sell call",
};

function isSoldOption(kind: NotionalKind): boolean {
  return kind === "SELL_PUT" || kind === "SELL_CALL";
}

function todayIso(): string {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

function notionalTableType(trade: NotionalTrade): string {
  if (trade.kind === "SHARES") return trade.quantity < 0 ? "STC" : "BTO";
  if (trade.kind === "SELL_PUT") return "Sell put";
  return "Sell call";
}

function notionalTypeColor(trade: NotionalTrade): string {
  if (trade.kind === "SHARES") {
    return trade.quantity < 0 ? TRADE_TYPE_COLOR.STC : TRADE_TYPE_COLOR.BTO;
  }
  if (trade.kind === "SELL_PUT") return TRADE_TYPE_COLOR["ROCT PUT"];
  return TRADE_TYPE_COLOR["ROCT CALL"];
}

function notionalAmount(trade: NotionalTrade): number {
  const absQty = Math.abs(trade.quantity);
  if (trade.kind === "SHARES") return trade.price * trade.quantity;
  if (!trade.assigned) return trade.premiumPerShare * absQty;
  const stockAmount = trade.price * absQty;
  return trade.kind === "SELL_CALL" ? -stockAmount : stockAmount;
}

function notionalStrike(trade: NotionalTrade): string | null {
  if (isSoldOption(trade.kind)) return formatMoney(String(trade.price));
  return null;
}

function notionalShareDisplay(trade: NotionalTrade): number {
  if (trade.kind === "SHARES") return trade.quantity;
  const absQty = Math.abs(trade.quantity);
  if (!trade.assigned) return absQty;
  return trade.kind === "SELL_CALL" ? -absQty : absQty;
}

interface DisplayTotals {
  totalShares: number;
  tradingShares: number;
  longTermShares: number;
  costBasis: number;
  premium: number;
  avgCostPerShare: number | null;
  costBasisPerShare: number | null;
}

function toNumber(value: string): number {
  const num = Number(value);
  return Number.isFinite(num) ? num : 0;
}

function totalsFromSummary(summary: PositionSummary): DisplayTotals {
  const totalShares = toNumber(summary.total.shares);
  const costBasis = toNumber(summary.total.cost_basis);
  const premium = toNumber(summary.total_premium_collected);
  return {
    totalShares,
    tradingShares: toNumber(summary.trading.shares),
    longTermShares: toNumber(summary.long_term.shares),
    costBasis,
    premium,
    avgCostPerShare: totalShares !== 0 ? costBasis / totalShares : null,
    // Spreadsheet Basis/sh: (costs − premiums) / shares held.
    costBasisPerShare: totalShares !== 0 ? (costBasis - premium) / totalShares : null,
  };
}

function applyNotional(totals: DisplayTotals, trade: NotionalTrade): DisplayTotals {
  const next = { ...totals };
  const absQty = Math.abs(trade.quantity);
  let shareDelta = 0;
  if (trade.kind === "SHARES") {
    shareDelta = trade.quantity;
  } else if (trade.assigned) {
    shareDelta = trade.kind === "SELL_PUT" ? absQty : -absQty;
  }
  const costDelta = shareDelta * trade.price;
  // Premium is stored once. Flipping Expired → Assigned only adds strike
  // × shares; it never double-counts premium.
  const premiumDelta = isSoldOption(trade.kind) ? trade.premiumPerShare * absQty : 0;

  next.totalShares += shareDelta;
  next.costBasis += costDelta;
  next.premium += premiumDelta;
  if (trade.shareClass === "trading") next.tradingShares += shareDelta;
  else next.longTermShares += shareDelta;

  next.avgCostPerShare = next.totalShares !== 0 ? next.costBasis / next.totalShares : null;
  next.costBasisPerShare =
    next.totalShares !== 0 ? (next.costBasis - next.premium) / next.totalShares : null;
  return next;
}

export default function TickerDrawer({
  ticker,
  accountName,
  onClose,
  onSelectTrade,
}: {
  ticker: string;
  accountName: string;
  onClose: () => void;
  onSelectTrade: (id: number) => void;
}) {
  const [summary, setSummary] = useState<PositionSummary | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notionals, setNotionals] = useState<NotionalTrade[]>([]);
  const [kind, setKind] = useState<NotionalKind>("SHARES");
  const [quantity, setQuantity] = useState("100");
  const [price, setPrice] = useState("");
  const [premiumPerShare, setPremiumPerShare] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setSummary(null);
    setTrades([]);
    setNotionals([]);

    Promise.all([
      getPositionSummary({ ticker, account: accountName }),
      listTrades({ ticker, account: accountName, limit: ALL_TRADES_LIMIT }),
    ])
      .then(([summaryData, tradesData]) => {
        if (cancelled) return;
        setSummary(summaryData);
        setTrades(tradesData.items);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load position.");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [ticker, accountName]);

  const displayed = useMemo(() => {
    if (!summary) return null;
    return notionals.reduce(applyNotional, totalsFromSummary(summary));
  }, [summary, notionals]);

  const soldOption = isSoldOption(kind);

  function addNotional() {
    const qty = Number(quantity);
    const px = Number(price);
    const premium = Number(premiumPerShare || 0);
    if (!Number.isFinite(qty) || qty === 0) return;
    if (!Number.isFinite(px) || px < 0) return;
    if (soldOption && (!Number.isFinite(premium) || premium < 0)) return;

    setNotionals((current) => [
      ...current,
      {
        id: crypto.randomUUID(),
        kind,
        shareClass: kind === "SHARES" ? "long_term" : "trading",
        quantity: soldOption ? Math.abs(qty) : qty,
        price: px,
        premiumPerShare: soldOption && Number.isFinite(premium) ? premium : 0,
        assigned: false,
        openedDate: todayIso(),
      },
    ]);
  }

  function setAssigned(id: string, assigned: boolean) {
    setNotionals((current) =>
      current.map((row) => (row.id === id ? { ...row, assigned } : row))
    );
  }

  return (
    <div className={styles.overlay} onClick={onClose}>
      <div
        className={`${styles.drawer} ${styles.drawerWide}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className={styles.drawerHeader}>
          <div>
            <div className={styles.drawerTitle}>{ticker}</div>
            <div className={styles.drawerSubtitle}>
              {accountName} · Lifetime position — all trades, any status
              {notionals.length > 0 ? " · includes simulation" : ""}
            </div>
          </div>
          <button type="button" className={styles.closeButton} onClick={onClose}>
            ✕
          </button>
        </div>

        {loading && <div className={styles.loading}>Loading position…</div>}
        {error && <div className={styles.error}>{error}</div>}

        {summary && displayed && (
          <>
            <div className={styles.positionSummaryGrid}>
              <PositionCard label="Total shares" value={formatShareCount(displayed.totalShares)} />
              <PositionCard
                label="Trading shares"
                value={formatShareCount(displayed.tradingShares)}
              />
              <PositionCard
                label="Long-term shares"
                value={formatShareCount(displayed.longTermShares)}
              />
              <PositionCard
                label="Total cost basis"
                value={formatMoney(String(displayed.costBasis - displayed.premium))}
              />
              <PositionCard
                label="Avg cost/share"
                value={
                  displayed.avgCostPerShare !== null
                    ? formatMoney(String(displayed.avgCostPerShare))
                    : "—"
                }
              />
              <PositionCard
                label="Cost basis/share"
                value={
                  displayed.costBasisPerShare !== null
                    ? formatMoney(String(displayed.costBasisPerShare))
                    : "—"
                }
                subvalue="costs − premiums, per share"
              />
              <PositionCard
                label="Premium collected"
                value={formatMoney(String(displayed.premium))}
                subvalue="lifetime, all statuses"
              />
            </div>

            <div className={styles.notionalPanel}>
              <div className={styles.sectionTitle}>Simulate a trade</div>
              <div className={styles.drawerSubtitle}>
                Notional only — never written to the trades database. For buy /
                sell, a negative share count is a sale. Sold puts/calls start
                expired (premium only); flip the row to Assigned to add strike
                cost without re-entering premium.
              </div>
              <form
                className={styles.notionalForm}
                onSubmit={(e) => {
                  e.preventDefault();
                  addNotional();
                }}
              >
                <label className={styles.notionalField}>
                  Type
                  <select
                    className={styles.select}
                    value={kind}
                    onChange={(e) => {
                      const next = e.target.value as NotionalKind;
                      setKind(next);
                      setQuantity("100");
                    }}
                  >
                    {Object.entries(NOTIONAL_KIND_LABEL).map(([value, label]) => (
                      <option key={value} value={value}>
                        {label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className={styles.notionalField}>
                  {kind === "SHARES" ? "Shares (+ buy / − sell)" : "Shares"}
                  <input
                    className={styles.searchInput}
                    type="number"
                    step="1"
                    value={quantity}
                    onChange={(e) => setQuantity(e.target.value)}
                  />
                </label>
                <label className={styles.notionalField}>
                  {soldOption ? "Strike" : "Price"}
                  <input
                    className={styles.searchInput}
                    type="number"
                    min="0"
                    step="0.01"
                    placeholder="0.00"
                    value={price}
                    onChange={(e) => setPrice(e.target.value)}
                  />
                </label>
                {soldOption && (
                  <label className={styles.notionalField}>
                    Premium
                    <input
                      className={styles.searchInput}
                      type="number"
                      min="0"
                      step="0.01"
                      placeholder="0.00"
                      value={premiumPerShare}
                      onChange={(e) => setPremiumPerShare(e.target.value)}
                    />
                  </label>
                )}
                <button type="submit" className={styles.presetButtonActive}>
                  Add to simulation
                </button>
              </form>
            </div>

            <div className={styles.sectionTitle}>
              All {ticker} trades ({trades.length + notionals.length})
            </div>
            <div className={styles.tableWrap}>
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th>Opened</th>
                    <th>Type</th>
                    <th className={styles.numeric}>Strike(s)</th>
                    <th>Expiration</th>
                    <th className={styles.numeric}>Shares</th>
                    <th className={styles.numeric}>Amount</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {[...notionals].reverse().map((trade) => (
                    <tr key={trade.id} className={styles.simRow}>
                      <td>{formatDate(trade.openedDate)}</td>
                      <td>
                        <span className={styles.typeCell}>
                          <span
                            className={styles.typeDot}
                            style={{ backgroundColor: notionalTypeColor(trade) }}
                          />
                          {notionalTableType(trade)}
                        </span>
                      </td>
                      <td className={styles.numeric}>{notionalStrike(trade) ?? "—"}</td>
                      <td>—</td>
                      <td className={styles.numeric}>
                        {formatShareCount(notionalShareDisplay(trade))}
                      </td>
                      <td className={styles.numeric}>
                        {formatMoney(String(notionalAmount(trade)))}
                      </td>
                      <td>
                        <span className={styles.statusWithAction}>
                          <span className={`${styles.badge} ${styles.badgeSim}`}>Sim</span>
                          {isSoldOption(trade.kind) && (
                            <span className={styles.outcomeToggle}>
                              <button
                                type="button"
                                className={`${styles.chip} ${!trade.assigned ? styles.chipActive : ""}`}
                                onClick={() => setAssigned(trade.id, false)}
                              >
                                Expired
                              </button>
                              <button
                                type="button"
                                className={`${styles.chip} ${trade.assigned ? styles.chipActive : ""}`}
                                onClick={() => setAssigned(trade.id, true)}
                              >
                                Assigned
                              </button>
                            </span>
                          )}
                          <button
                            type="button"
                            className={styles.clearButton}
                            onClick={() =>
                              setNotionals((current) =>
                                current.filter((row) => row.id !== trade.id)
                              )
                            }
                          >
                            Remove
                          </button>
                        </span>
                      </td>
                    </tr>
                  ))}
                  {trades.map((trade) => (
                    <tr key={trade.id} onClick={() => onSelectTrade(trade.id)}>
                      <td>{formatDate(trade.date_trade_open)}</td>
                      <td>
                        <span className={styles.typeCell}>
                          <span
                            className={styles.typeDot}
                            style={{
                              backgroundColor:
                                TRADE_TYPE_COLOR[trade.trade_type] ?? DEFAULT_TRADE_TYPE_COLOR,
                            }}
                          />
                          {trade.trade_type}
                        </span>
                      </td>
                      <td className={styles.numeric}>
                        {trade.long_strike
                          ? `${formatMoney(trade.strike_price)}/${formatMoney(trade.long_strike)}`
                          : trade.strike_price
                            ? formatMoney(trade.strike_price)
                            : "—"}
                      </td>
                      <td>{formatDate(trade.expiration_date)}</td>
                      <td className={styles.numeric}>
                        {formatShareCount(tradeShareCount(trade))}
                      </td>
                      <td className={styles.numeric}>
                        {tradeAmount(trade) === null
                          ? "—"
                          : formatMoney(String(tradeAmount(trade)))}
                      </td>
                      <td>
                        <span
                          className={`${styles.badge} ${styles[DISPLAY_STATUS_BADGE_CLASS[trade.display_status]]}`}
                        >
                          {DISPLAY_STATUS_LABEL[trade.display_status]}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {trades.length === 0 && notionals.length === 0 && (
                <div className={styles.emptyState}>No trades found for this ticker.</div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function PositionCard({
  label,
  value,
  subvalue,
}: {
  label: string;
  value: string;
  subvalue?: string;
}) {
  return (
    <div className={styles.kpiCard}>
      <span className={styles.kpiLabel}>{label}</span>
      <span className={styles.kpiValue}>{value}</span>
      {subvalue && <span className={styles.kpiSubvalue}>{subvalue}</span>}
    </div>
  );
}
