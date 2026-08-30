"use client";

import { FormEvent, useState } from "react";
import {
  calculateArorc,
  calculateCostBasis,
  calculateKelly,
  calculateRorc,
} from "@/lib/api-client";
import styles from "./calculators.module.css";

function Field({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  return (
    <label className={styles.field}>
      <span className={styles.label}>{label}</span>
      <input
        className={styles.input}
        type="text"
        inputMode="decimal"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}

function RorcCard() {
  const [netCreditPerShare, setNetCreditPerShare] = useState("0.8259");
  const [riskCapitalPerShare, setRiskCapitalPerShare] = useState("216.6741");
  const [marginPercent, setMarginPercent] = useState("100");
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const response = await calculateRorc({
        net_credit_per_share: netCreditPerShare,
        risk_capital_per_share: riskCapitalPerShare,
        margin_percent: marginPercent,
      });
      setResult(response.rorc);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Calculation failed.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className={styles.card}>
      <h2 className={styles.cardTitle}>RORC</h2>
      <p className={styles.cardDescription}>
        Return on Risk Capital — net credit per share divided by effective
        risk capital per share.
      </p>
      <form className={styles.form} onSubmit={onSubmit}>
        <Field
          label="Net credit per share"
          value={netCreditPerShare}
          onChange={setNetCreditPerShare}
          placeholder="0.8259"
        />
        <Field
          label="Risk capital per share"
          value={riskCapitalPerShare}
          onChange={setRiskCapitalPerShare}
          placeholder="216.6741"
        />
        <Field
          label="Margin percent (default 100)"
          value={marginPercent}
          onChange={setMarginPercent}
          placeholder="100"
        />
        <button className={styles.button} type="submit" disabled={loading}>
          {loading ? "Calculating…" : "Calculate"}
        </button>
      </form>
      {result !== null && (
        <div className={styles.result}>
          <div className={styles.resultRow}>
            <span className={styles.resultLabel}>RORC</span>
            <span className={styles.resultValue}>{result}</span>
          </div>
        </div>
      )}
      {error && <div className={styles.error}>{error}</div>}
    </div>
  );
}

function ArorcCard() {
  const [rorc, setRorc] = useState("0.0038117153826876396");
  const [daysToExpiration, setDaysToExpiration] = useState("7");
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const response = await calculateArorc({
        rorc,
        days_to_expiration: Number(daysToExpiration),
      });
      setResult(response.arorc);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Calculation failed.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className={styles.card}>
      <h2 className={styles.cardTitle}>ARORC</h2>
      <p className={styles.cardDescription}>
        Annualized Return on Risk Capital — RORC scaled by 365 / days to
        expiration.
      </p>
      <form className={styles.form} onSubmit={onSubmit}>
        <Field
          label="RORC"
          value={rorc}
          onChange={setRorc}
          placeholder="0.0038117153826876396"
        />
        <Field
          label="Days to expiration"
          value={daysToExpiration}
          onChange={setDaysToExpiration}
          placeholder="7"
        />
        <button className={styles.button} type="submit" disabled={loading}>
          {loading ? "Calculating…" : "Calculate"}
        </button>
      </form>
      {result !== null && (
        <div className={styles.result}>
          <div className={styles.resultRow}>
            <span className={styles.resultLabel}>ARORC</span>
            <span className={styles.resultValue}>{result}</span>
          </div>
        </div>
      )}
      {error && <div className={styles.error}>{error}</div>}
    </div>
  );
}

function KellyCard() {
  const [probabilityOfWinning, setProbabilityOfWinning] = useState("0.7");
  const [avgWin, setAvgWin] = useState("2");
  const [avgLoss, setAvgLoss] = useState("1");
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const response = await calculateKelly({
        probability_of_winning: probabilityOfWinning,
        avg_win: avgWin,
        avg_loss: avgLoss,
      });
      setResult(response.kelly);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Calculation failed.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className={styles.card}>
      <h2 className={styles.cardTitle}>Kelly Criterion</h2>
      <p className={styles.cardDescription}>
        Position sizing fraction: W − (1 − W) / (avg win / avg loss).
      </p>
      <form className={styles.form} onSubmit={onSubmit}>
        <Field
          label="Probability of winning (W)"
          value={probabilityOfWinning}
          onChange={setProbabilityOfWinning}
          placeholder="0.7"
        />
        <Field
          label="Average win"
          value={avgWin}
          onChange={setAvgWin}
          placeholder="2"
        />
        <Field
          label="Average loss"
          value={avgLoss}
          onChange={setAvgLoss}
          placeholder="1"
        />
        <button className={styles.button} type="submit" disabled={loading}>
          {loading ? "Calculating…" : "Calculate"}
        </button>
      </form>
      {result !== null && (
        <div className={styles.result}>
          <div className={styles.resultRow}>
            <span className={styles.resultLabel}>Kelly fraction</span>
            <span className={styles.resultValue}>{result}</span>
          </div>
        </div>
      )}
      {error && <div className={styles.error}>{error}</div>}
    </div>
  );
}

function CostBasisCard() {
  const [priorRunningBasis, setPriorRunningBasis] = useState("0");
  const [priorRunningShares, setPriorRunningShares] = useState("0");
  const [totalAmount, setTotalAmount] = useState("1000.00");
  const [shares, setShares] = useState("100");
  const [result, setResult] = useState<{
    running_basis: string;
    running_shares: string;
    basis_per_share: string | null;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const response = await calculateCostBasis({
        prior_running_basis: priorRunningBasis,
        prior_running_shares: priorRunningShares,
        total_amount: totalAmount,
        shares,
      });
      setResult(response);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Calculation failed.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className={styles.card}>
      <h2 className={styles.cardTitle}>Cost Basis</h2>
      <p className={styles.cardDescription}>
        Folds one transaction into prior running totals. Use negative amount
        and shares for a sell-to-close.
      </p>
      <form className={styles.form} onSubmit={onSubmit}>
        <Field
          label="Prior running basis"
          value={priorRunningBasis}
          onChange={setPriorRunningBasis}
          placeholder="0"
        />
        <Field
          label="Prior running shares"
          value={priorRunningShares}
          onChange={setPriorRunningShares}
          placeholder="0"
        />
        <Field
          label="Total amount (this transaction)"
          value={totalAmount}
          onChange={setTotalAmount}
          placeholder="1000.00"
        />
        <Field
          label="Shares (this transaction)"
          value={shares}
          onChange={setShares}
          placeholder="100"
        />
        <button className={styles.button} type="submit" disabled={loading}>
          {loading ? "Calculating…" : "Calculate"}
        </button>
      </form>
      {result !== null && (
        <div className={styles.result}>
          <div className={styles.resultRow}>
            <span className={styles.resultLabel}>Running basis</span>
            <span className={styles.resultValue}>{result.running_basis}</span>
          </div>
          <div className={styles.resultRow}>
            <span className={styles.resultLabel}>Running shares</span>
            <span className={styles.resultValue}>
              {result.running_shares}
            </span>
          </div>
          <div className={styles.resultRow}>
            <span className={styles.resultLabel}>Basis per share</span>
            <span className={styles.resultValue}>
              {result.basis_per_share ?? "—"}
            </span>
          </div>
        </div>
      )}
      {error && <div className={styles.error}>{error}</div>}
    </div>
  );
}

export default function CalculatorsPage() {
  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <h1 className={styles.title}>Calculator Sandbox</h1>
        <p className={styles.subtitle}>
          Test the RORC, ARORC, Kelly Criterion, and cost-basis calculations
          against the running backend.
        </p>
      </div>
      <div className={styles.grid}>
        <RorcCard />
        <ArorcCard />
        <KellyCard />
        <CostBasisCard />
      </div>
    </div>
  );
}
