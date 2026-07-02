import { useEffect, useState } from "react";
import { Activity, Coins, Cpu } from "lucide-react";

import { getUsageSummary, type UsageScope, type UsageSummary } from "../../lib/api";
import { UsageChart } from "./UsageChart";
import { formatCost, formatDayLabel, formatTokenCount } from "./format";

type UsagePanelProps = {
  scope?: UsageScope;
  scopeId?: string;
  title?: string;
};

const DIMENSION_LABEL: Record<UsageSummary["breakdown_dimension"], string> = {
  project: "By project",
  team: "By team",
  workspace: "By workspace",
  user: "By user",
};

type State =
  | { status: "loading" }
  | { status: "ready"; data: UsageSummary }
  | { status: "error"; message: string };

export function UsagePanel({ scope, scopeId, title = "Usage & cost" }: UsagePanelProps) {
  const [state, setState] = useState<State>({ status: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    getUsageSummary({ scope, id: scopeId }, controller.signal)
      .then((data) => setState({ status: "ready", data }))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setState({
          status: "error",
          message: error instanceof Error ? error.message : "Unable to load usage",
        });
      });
    return () => controller.abort();
  }, [scope, scopeId]);

  return (
    <section className="rounded-md border border-line bg-white p-5 shadow-soft">
      <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium uppercase tracking-[0.16em] text-ink/55">Analytics</p>
          <h3 className="mt-1 text-2xl font-semibold">{title}</h3>
        </div>
      </div>

      {state.status === "loading" && (
        <div className="rounded-md border border-dashed border-line bg-white p-6 text-ink/60">
          Loading usage…
        </div>
      )}
      {state.status === "error" && (
        <div className="rounded-md border border-danger/30 bg-danger/5 px-4 py-3 text-sm text-danger">
          {state.message}
        </div>
      )}
      {state.status === "ready" && <UsageContent data={state.data} />}
    </section>
  );
}

const EMPTY_TOTALS: UsageSummary["totals"] = {
  run_count: 0,
  input_tokens: 0,
  output_tokens: 0,
  tokens_used: 0,
  estimated_cost: 0,
};

function UsageContent({ data }: { data: UsageSummary }) {
  // Read defensively so a malformed/partial payload never crashes the whole page.
  const totals = data.totals ?? EMPTY_TOTALS;
  const series = data.series ?? [];
  const breakdown = data.breakdown ?? [];
  const topSpenders = data.top_spenders ?? [];
  const dimension = data.breakdown_dimension ?? "project";

  const chartPoints = series.map((point) => ({
    label: formatDayLabel(point.date),
    value: point.estimated_cost,
    tooltip: `${formatDayLabel(point.date)} · ${formatCost(point.estimated_cost)} · ${formatTokenCount(
      point.tokens_used,
    )} tokens`,
  }));

  return (
    <div className="flex flex-col gap-6">
      <div className="grid gap-3 sm:grid-cols-3">
        <StatTile icon={<Coins className="h-4 w-4" />} label="Estimated cost">
          {formatCost(totals.estimated_cost)}
        </StatTile>
        <StatTile icon={<Cpu className="h-4 w-4" />} label="Tokens">
          {formatTokenCount(totals.tokens_used)}
        </StatTile>
        <StatTile icon={<Activity className="h-4 w-4" />} label="Runs">
          {totals.run_count.toLocaleString()}
        </StatTile>
      </div>

      <div>
        <p className="mb-2 text-xs font-semibold uppercase tracking-[0.14em] text-muted">
          Cost per day
        </p>
        <UsageChart points={chartPoints} ariaLabel="Estimated cost per day" />
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <BreakdownList heading={DIMENSION_LABEL[dimension]} items={breakdown} />
        <BreakdownList heading="Top spenders" items={topSpenders} />
      </div>
    </div>
  );
}

function StatTile({
  icon,
  label,
  children,
}: {
  icon: React.ReactNode;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-line bg-panel p-4">
      <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-[0.12em] text-ink/55">
        <span className="text-primary">{icon}</span>
        {label}
      </div>
      <p className="mt-2 text-2xl font-semibold text-ink">{children}</p>
    </div>
  );
}

function BreakdownList({
  heading,
  items,
}: {
  heading: string;
  items: UsageSummary["breakdown"];
}) {
  return (
    <div>
      <p className="mb-2 text-xs font-semibold uppercase tracking-[0.14em] text-muted">{heading}</p>
      {items.length === 0 ? (
        <p className="rounded-md border border-dashed border-line px-3 py-4 text-sm text-ink/55">
          No data.
        </p>
      ) : (
        <ul className="divide-y divide-line rounded-md border border-line">
          {items.map((item) => (
            <li
              key={item.id ?? item.name}
              className="flex items-center justify-between gap-3 px-3 py-2 text-sm"
            >
              <span className="min-w-0 flex-1 truncate text-ink">{item.name}</span>
              <span className="text-ink/60">{formatTokenCount(item.tokens_used)}</span>
              <span className="w-16 text-right font-medium text-ink">
                {formatCost(item.estimated_cost)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
