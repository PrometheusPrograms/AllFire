import { useEffect, useRef } from "react";
import type { ReactNode } from "react";
import type { Trade } from "@/lib/api-client";
import FormattedDate from "./FormattedDate";
import {
  formatMoney,
  formatPercent,
  formatShareCount,
  netCreditTotal,
  tradeShareCount,
} from "./format";
import styles from "./trades.module.css";

const NAKED_OPTION_TYPES = new Set([
  "ROCT PUT",
  "ROCT CALL",
  "RULE ONE PUT",
  "RULE ONE CALL",
]);

type SheetRow = {
  label: string;
  value: (leg: Trade) => ReactNode;
};

const BASE_ROWS: SheetRow[] = [
  {
    label: "TRADE DATE",
    value: (leg): ReactNode => <FormattedDate value={leg.date_trade_open} />,
  },
  { label: "PRICE", value: (leg) => formatMoney(leg.price_per_share) },
  { label: "DTE", value: (leg) => leg.days_to_expiration ?? "—" },
  {
    label: "EXPIRATION DATE",
    value: (leg) => <FormattedDate value={leg.expiration_date} />,
  },
  {
    label: "PROB OTM",
    value: (leg) => formatPercent(leg.probability_of_winning),
  },
  { label: "DELTA", value: (leg) => formatPlain(leg.delta) },
  { label: "SHORT STRIKE", value: (leg) => formatMoney(leg.strike_price) },
];

const LONG_STRIKE_ROW: SheetRow = {
  label: "LONG STRIKE",
  value: (leg) => formatMoney(leg.long_strike),
};

const AFTER_STRIKE_ROWS: SheetRow[] = [
  { label: "CREDIT/(DEBIT)", value: (leg) => formatMoney(leg.credit_debit) },
  {
    label: "SHARES",
    value: (leg) => formatShareCount(tradeShareCount(leg)),
  },
  {
    label: "NET CREDIT TOTAL",
    value: (leg) => formatMoney(netCreditTotal(leg)),
  },
  { label: "ARORC", value: (leg) => formatPercent(leg.arorc) },
  { label: "RESULT", value: (leg) => leg.result ?? "—" },
  {
    label: "RESULT DATE",
    value: (leg) => <FormattedDate value={leg.result_date} />,
  },
  { label: "CLOSING DEBIT", value: (leg) => formatMoney(leg.closing_debit) },
  { label: "TOTAL DEBIT", value: (leg) => formatMoney(leg.total_debit) },
  {
    label: "NET CREDIT/(DEBIT)",
    value: (leg) => formatMoney(leg.result_net_credit),
  },
  { label: "FINAL ARORC", value: (leg) => formatPercent(leg.final_arorc) },
];

function formatPlain(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const num = Number(value);
  if (Number.isNaN(num)) return "—";
  return num.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function showLongStrike(legs: Trade[]): boolean {
  return legs.some((leg) => !NAKED_OPTION_TYPES.has(leg.trade_type));
}

function sheetRows(legs: Trade[]): SheetRow[] {
  return [
    ...BASE_ROWS,
    ...(showLongStrike(legs) ? [LONG_STRIKE_ROW] : []),
    ...AFTER_STRIKE_ROWS,
  ];
}

export default function OkwColumnSheet({
  legs,
  focusedId,
  onSelectLeg,
}: {
  legs: Trade[];
  focusedId: number;
  onSelectLeg: (id: number) => void;
}) {
  const rows = sheetRows(legs);
  const colRefs = useRef<Map<number, HTMLTableCellElement>>(new Map());

  useEffect(() => {
    colRefs.current.get(focusedId)?.scrollIntoView({
      inline: "center",
      block: "nearest",
      behavior: "smooth",
    });
  }, [focusedId, legs.length]);

  return (
    <div className={styles.okwSheetBlock}>
      <div
        className={`${styles.okwSheetWrap} ${legs.length > 5 ? styles.okwSheetOverflow : ""}`}
      >
        <table className={styles.okwSheet}>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={row.label}>
                <th className={styles.okwLabel} scope="row">
                  {row.label}
                </th>
                {legs.map((leg) => (
                  <td
                    key={leg.id}
                    ref={
                      rowIndex === 0
                        ? (node) => {
                            if (node) colRefs.current.set(leg.id, node);
                            else colRefs.current.delete(leg.id);
                          }
                        : undefined
                    }
                    className={leg.id === focusedId ? styles.okwColActive : undefined}
                  >
                    <button
                      type="button"
                      className={styles.okwCell}
                      onClick={() => onSelectLeg(leg.id)}
                    >
                      {row.value(leg)}
                    </button>
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
