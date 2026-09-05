import { useCallback, useEffect, useState } from "react";

import {
  createBudget,
  deleteBudget,
  listBudgets,
  updateBudget,
  type Budget,
  type BudgetPeriod,
  type BudgetStatus,
  type UsageScope,
} from "../../lib/api";
import { formatCost } from "./format";

type BudgetConfigProps = {
  scope: UsageScope;
  scopeId: string;
  canManage: boolean;
};

const PERIODS: BudgetPeriod[] = ["daily", "weekly", "monthly", "total"];

const STATUS_STYLES: Record<BudgetStatus, string> = {
  ok: "bg-signal/15 text-signal",
  warning: "bg-warn/15 text-warn",
  over: "bg-danger/15 text-danger",
};

export function BudgetConfig({ scope, scopeId, canManage }: BudgetConfigProps) {
  const [budgets, setBudgets] = useState<Budget[]>([]);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    (signal?: AbortSignal) => {
      setStatus("loading");
      listBudgets(scope, scopeId, signal)
        .then((data) => {
          setBudgets(data);
          setStatus("ready");
        })
        .catch((err: unknown) => {
          if (err instanceof DOMException && err.name === "AbortError") return;
          setError(err instanceof Error ? err.message : "Unable to load budgets");
          setStatus("error");
        });
    },
    [scope, scopeId],
  );

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  return (
    <section className="rounded-md border border-line bg-white p-5 shadow-soft">
      <div className="mb-4">
        <p className="text-sm font-medium uppercase tracking-[0.16em] text-ink/55">Guardrails</p>
        <h3 className="mt-1 text-xl font-semibold">Budgets</h3>
      </div>

      {status === "loading" && <p className="text-sm text-ink/60">Loading budgets…</p>}
      {status === "error" && (
        <p className="rounded-md border border-danger/30 bg-danger/5 px-4 py-3 text-sm text-danger">
          {error}
        </p>
      )}

      {status === "ready" && (
        <div className="flex flex-col gap-4">
          {budgets.length === 0 ? (
            <p className="rounded-md border border-dashed border-line px-3 py-4 text-sm text-ink/55">
              No budgets configured.
            </p>
          ) : (
            <ul className="flex flex-col gap-3">
              {budgets.map((budget) => (
                <BudgetRow
                  key={budget.id}
                  budget={budget}
                  canManage={canManage}
                  onChanged={() => load()}
                />
              ))}
            </ul>
          )}

          {canManage && <BudgetCreateForm scope={scope} scopeId={scopeId} onCreated={() => load()} />}
        </div>
      )}
    </section>
  );
}

function progressPercent(budget: Budget): number {
  if (budget.limit_cost && budget.limit_cost > 0) {
    return Math.min(100, (budget.current_cost / budget.limit_cost) * 100);
  }
  if (budget.limit_tokens && budget.limit_tokens > 0) {
    return Math.min(100, (budget.current_tokens / budget.limit_tokens) * 100);
  }
  return 0;
}

function BudgetRow({
  budget,
  canManage,
  onChanged,
}: {
  budget: Budget;
  canManage: boolean;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const limitLabel =
    budget.limit_cost != null
      ? formatCost(budget.limit_cost)
      : `${budget.limit_tokens?.toLocaleString()} tokens`;

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Action failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="rounded-md border border-line p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span
            className={`rounded-full px-2.5 py-0.5 text-xs font-semibold uppercase tracking-[0.06em] ${STATUS_STYLES[budget.status]}`}
          >
            {budget.status}
          </span>
          <span className="text-sm font-medium capitalize text-ink">{budget.period}</span>
          <span className="text-sm text-ink/60">
            {formatCost(budget.current_cost)} / {limitLabel}
          </span>
          {budget.enforce && (
            <span className="rounded bg-ink/10 px-1.5 py-0.5 text-[11px] font-semibold uppercase text-ink/70">
              Enforced
            </span>
          )}
        </div>
        {canManage && (
          <div className="flex items-center gap-3 text-sm">
            <button
              type="button"
              disabled={busy}
              onClick={() => act(() => updateBudget(budget.id, { enforce: !budget.enforce }))}
              className="font-medium text-primary transition hover:text-primary-active disabled:text-muted-soft"
            >
              {budget.enforce ? "Disable block" : "Enforce"}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => act(() => deleteBudget(budget.id))}
              className="font-medium text-danger transition hover:opacity-80 disabled:text-muted-soft"
            >
              Delete
            </button>
          </div>
        )}
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-line">
        <div
          className={`h-full rounded-full ${budget.status === "over" ? "bg-danger" : budget.status === "warning" ? "bg-warn" : "bg-signal"}`}
          style={{ width: `${progressPercent(budget)}%` }}
        />
      </div>
      {error && <p className="mt-2 text-xs text-danger">{error}</p>}
    </li>
  );
}

function BudgetCreateForm({
  scope,
  scopeId,
  onCreated,
}: {
  scope: UsageScope;
  scopeId: string;
  onCreated: () => void;
}) {
  const [period, setPeriod] = useState<BudgetPeriod>("monthly");
  const [limitCost, setLimitCost] = useState("");
  const [threshold, setThreshold] = useState("80");
  const [enforce, setEnforce] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const cost = Number(limitCost);
    if (!Number.isFinite(cost) || cost <= 0) {
      setError("Enter a cost limit greater than 0");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await createBudget({
        scope,
        scope_id: scopeId,
        period,
        limit_cost: cost,
        threshold_percent: Number(threshold),
        enforce,
      });
      setLimitCost("");
      setEnforce(false);
      onCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to create budget");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="rounded-md border border-dashed border-line p-3">
      <p className="mb-3 text-sm font-semibold text-ink">New budget</p>
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-xs font-medium text-ink/60">
          Period
          <select
            value={period}
            onChange={(e) => setPeriod(e.target.value as BudgetPeriod)}
            className="mt-1 block rounded-md border border-line bg-white px-2 py-1.5 text-sm text-ink"
          >
            {PERIODS.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs font-medium text-ink/60">
          Cost limit (USD)
          <input
            type="number"
            min="0"
            step="0.01"
            value={limitCost}
            onChange={(e) => setLimitCost(e.target.value)}
            placeholder="50.00"
            className="mt-1 block w-28 rounded-md border border-line bg-white px-2 py-1.5 text-sm text-ink"
          />
        </label>
        <label className="text-xs font-medium text-ink/60">
          Warn at (%)
          <input
            type="number"
            min="0"
            max="100"
            value={threshold}
            onChange={(e) => setThreshold(e.target.value)}
            className="mt-1 block w-20 rounded-md border border-line bg-white px-2 py-1.5 text-sm text-ink"
          />
        </label>
        <label className="flex items-center gap-1.5 text-sm text-ink/70">
          <input
            type="checkbox"
            checked={enforce}
            onChange={(e) => setEnforce(e.target.checked)}
          />
          Block over limit
        </label>
        <button
          type="submit"
          disabled={busy}
          className="rounded-md bg-primary px-4 py-2 text-sm font-semibold text-white transition hover:bg-primary-active disabled:bg-muted-soft"
        >
          {busy ? "Saving…" : "Add budget"}
        </button>
      </div>
      {error && <p className="mt-2 text-xs text-danger">{error}</p>}
    </form>
  );
}
