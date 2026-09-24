import { formatDate, formatDateParts } from "./format";
import styles from "./trades.module.css";

/** Tabular date so `07 AUG 26` and `31 JUL 26` occupy the same width and
 * line up down a column (proportional fonts make AUG wider than JUL). */
export default function FormattedDate({
  value,
}: {
  value: string | null | undefined;
}) {
  const parts = formatDateParts(value);
  if (!parts) return "—";
  return (
    <span className={styles.dateCell} aria-label={formatDate(value)}>
      <span className={styles.dateDay} aria-hidden>
        {parts.day}
      </span>
      <span className={styles.dateMon} aria-hidden>
        {parts.month}
      </span>
      <span className={styles.dateYear} aria-hidden>
        {parts.year}
      </span>
    </span>
  );
}
