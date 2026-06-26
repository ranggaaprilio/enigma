import { useCallback, useEffect, useState, type ComponentType } from "react";
import { Link } from "react-router";
import {
  Activity,
  ArrowRight,
  Boxes,
  FolderKanban,
  RefreshCw,
  Users,
} from "lucide-react";

import { AppShell } from "../components/AppShell";
import {
  getDashboardSummary,
  getHealth,
  type DashboardSummary,
  type HealthResponse,
  type WorkspaceStatus,
} from "../lib/api";

const SUBSYSTEMS: { key: keyof Omit<HealthResponse, "status">; label: string }[] = [
  { key: "database", label: "Database" },
  { key: "redis", label: "Redis" },
  { key: "daytona", label: "Daytona" },
];

const STATUS_ORDER: WorkspaceStatus[] = [
  "ready",
  "busy",
  "creating",
  "stopped",
  "error",
];

const statusTone: Record<WorkspaceStatus, string> = {
  ready: "bg-[#f0faf5] text-signal ring-1 ring-signal/30",
  busy: "bg-[#fdf6ec] text-warn ring-1 ring-warn/30",
  creating: "bg-white text-ink ring-1 ring-line",
  stopped: "bg-white text-muted ring-1 ring-line",
  error: "bg-[#fff5f2] text-danger ring-1 ring-danger/30",
};

export function DashboardOverviewRoute() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthFailed, setHealthFailed] = useState(false);
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    const [healthResult, summaryResult] = await Promise.allSettled([
      getHealth(signal),
      getDashboardSummary(signal),
    ]);
    if (signal?.aborted) return;

    if (healthResult.status === "fulfilled") {
      setHealth(healthResult.value);
      setHealthFailed(false);
    } else {
      setHealth(null);
      setHealthFailed(true);
    }

    if (summaryResult.status === "fulfilled") {
      setSummary(summaryResult.value);
    } else {
      const reason = summaryResult.reason;
      setError(reason instanceof Error ? reason.message : "Failed to load dashboard");
    }

    setLastUpdated(new Date());
    setLoading(false);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const overall = healthFailed
    ? { label: "Unavailable", tone: "bg-[#fff5f2] text-danger" }
    : health?.status === "ok"
      ? { label: "Operational", tone: "bg-white text-signal ring-1 ring-line" }
      : health?.status === "degraded"
        ? { label: "Degraded", tone: "bg-white text-warn ring-1 ring-line" }
        : { label: "Checking…", tone: "bg-white text-muted ring-1 ring-line" };

  return (
    <AppShell>
      <main className="mx-auto w-full max-w-5xl px-6 py-10">
        <div className="mb-8 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold text-ink">Dashboard</h1>
            <p className="mt-1 text-sm text-muted">
              {lastUpdated
                ? `Last updated ${lastUpdated.toLocaleTimeString()}`
                : "Loading…"}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => void load()}
              disabled={loading}
              className="inline-flex items-center gap-2 rounded-md border border-line bg-white px-3 py-2 text-sm font-medium text-ink transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-60"
            >
              <RefreshCw
                className={`h-4 w-4 ${loading ? "animate-spin" : ""}`}
                aria-hidden="true"
              />
              Refresh
            </button>
            <Link
              to="/projects"
              className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-white transition hover:bg-primary-active"
            >
              View all projects
              <ArrowRight className="h-4 w-4" aria-hidden="true" />
            </Link>
          </div>
        </div>

        {error && (
          <div className="mb-6 rounded-md border border-danger/30 bg-[#fff5f2] px-4 py-3 text-sm text-danger">
            {error}
          </div>
        )}

        {/* System health */}
        <section className="mb-8 rounded-xl border border-line bg-panel p-5">
          <div className="mb-4 flex items-center justify-between gap-3">
            <h2 className="flex items-center gap-2 text-base font-semibold text-ink">
              <Activity className="h-4 w-4 text-primary" aria-hidden="true" />
              System health
            </h2>
            <span
              className={`rounded-full px-3 py-1 text-sm font-semibold ${overall.tone}`}
            >
              {overall.label}
            </span>
          </div>
          <ul className="grid gap-3 sm:grid-cols-2">
            <HealthRow
              label="Backend"
              value={healthFailed ? "error" : health ? "ok" : "pending"}
            />
            {SUBSYSTEMS.map(({ key, label }) => (
              <HealthRow
                key={key}
                label={label}
                value={healthFailed ? "error" : health ? health[key] : "pending"}
              />
            ))}
          </ul>
        </section>

        {/* Totals */}
        <section className="mb-8 grid gap-4 sm:grid-cols-3">
          <StatCard
            icon={FolderKanban}
            label="Projects"
            value={summary?.projects_total}
          />
          <StatCard icon={Users} label="Teams" value={summary?.teams_total} />
          <StatCard
            icon={Boxes}
            label="Workspaces"
            value={summary?.workspaces_total}
          />
        </section>

        {/* Workspace status breakdown */}
        <section className="mb-8 rounded-xl border border-line bg-panel p-5">
          <h2 className="mb-4 text-base font-semibold text-ink">
            Workspace status
          </h2>
          {summary && summary.workspaces_total > 0 ? (
            <div className="flex flex-wrap gap-2">
              {STATUS_ORDER.map((s) => (
                <span
                  key={s}
                  className={`rounded-full px-3 py-1 text-sm font-medium ${statusTone[s]}`}
                >
                  {summary.workspaces_by_status[s]} {s}
                </span>
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted">
              {summary ? "No workspaces yet." : "Loading…"}
            </p>
          )}
        </section>

        <div className="grid gap-6 lg:grid-cols-2">
          {/* Your projects */}
          <section>
            <h2 className="mb-3 text-base font-semibold text-ink">Your projects</h2>
            {summary && summary.recent_projects.length > 0 ? (
              <div className="space-y-3">
                {summary.recent_projects.map((p) => (
                  <Link
                    key={p.id}
                    to={`/projects/${p.id}`}
                    className="group block rounded-xl border border-line bg-panel p-4 transition hover:border-primary hover:shadow-sm"
                  >
                    <div className="flex items-center justify-between gap-3">
                      <h3 className="font-semibold text-ink group-hover:text-primary">
                        {p.name}
                      </h3>
                      <ArrowRight className="h-4 w-4 shrink-0 text-muted group-hover:text-primary" aria-hidden="true" />
                    </div>
                    {p.description && (
                      <p className="mt-1 line-clamp-1 text-sm text-body">
                        {p.description}
                      </p>
                    )}
                    <p className="mt-2 text-xs text-muted">
                      {p.team_count} {p.team_count === 1 ? "team" : "teams"} ·{" "}
                      {p.workspace_count}{" "}
                      {p.workspace_count === 1 ? "workspace" : "workspaces"}
                    </p>
                  </Link>
                ))}
              </div>
            ) : (
              <div className="rounded-xl border border-line bg-panel p-6 text-center text-sm text-body">
                {summary ? (
                  <>
                    No projects yet.{" "}
                    <Link to="/projects" className="font-medium text-primary hover:underline">
                      Create your first project
                    </Link>
                  </>
                ) : (
                  "Loading…"
                )}
              </div>
            )}
          </section>

          {/* Recent workspaces */}
          <section>
            <h2 className="mb-3 text-base font-semibold text-ink">
              Recent workspaces
            </h2>
            {summary && summary.recent_workspaces.length > 0 ? (
              <div className="space-y-3">
                {summary.recent_workspaces.map((w) => {
                  const linkable = w.project_id && w.team_id;
                  const inner = (
                    <>
                      <div className="flex items-center justify-between gap-3">
                        <h3 className="font-semibold text-ink">{w.name}</h3>
                        <span
                          className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${statusTone[w.status]}`}
                        >
                          {w.status}
                        </span>
                      </div>
                      <p className="mt-1 text-xs text-muted">
                        {w.project_name && w.team_name
                          ? `${w.project_name} / ${w.team_name}`
                          : "Personal workspace"}
                      </p>
                    </>
                  );
                  return linkable ? (
                    <Link
                      key={w.id}
                      to={`/projects/${w.project_id}/teams/${w.team_id}/workspaces/${w.id}`}
                      className="block rounded-xl border border-line bg-panel p-4 transition hover:border-primary hover:shadow-sm"
                    >
                      {inner}
                    </Link>
                  ) : (
                    <div
                      key={w.id}
                      className="rounded-xl border border-line bg-panel p-4"
                    >
                      {inner}
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="rounded-xl border border-line bg-panel p-6 text-center text-sm text-body">
                {summary ? "No workspaces yet." : "Loading…"}
              </div>
            )}
          </section>
        </div>
      </main>
    </AppShell>
  );
}

function HealthRow({
  label,
  value,
}: {
  label: string;
  value: "ok" | "error" | "pending";
}) {
  const tone =
    value === "ok"
      ? { dot: "bg-signal", text: "text-signal" }
      : value === "error"
        ? { dot: "bg-danger", text: "text-danger" }
        : { dot: "bg-muted-soft", text: "text-muted" };
  return (
    <li className="flex items-center justify-between gap-3 rounded-md border border-line bg-white px-3 py-2">
      <span className="flex items-center gap-2 text-sm text-ink">
        <span className={`h-2.5 w-2.5 rounded-full ${tone.dot}`} aria-hidden="true" />
        {label}
      </span>
      <span className={`text-sm font-medium capitalize ${tone.text}`}>{value}</span>
    </li>
  );
}

function StatCard({
  icon: Icon,
  label,
  value,
}: {
  icon: ComponentType<{ className?: string }>;
  label: string;
  value: number | undefined;
}) {
  return (
    <div className="rounded-xl border border-line bg-panel p-5">
      <div className="flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-md bg-primary/10 text-primary">
          <Icon className="h-5 w-5" />
        </div>
        <div>
          <p className="text-sm text-muted">{label}</p>
          <p className="text-2xl font-semibold text-ink">
            {value ?? "—"}
          </p>
        </div>
      </div>
    </div>
  );
}
