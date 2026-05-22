import { useEffect, useMemo, useState } from "react";

import { useAuth } from "../auth/context";
import { DashboardShell } from "../components/DashboardShell";
import { getHealth, type HealthResponse } from "../lib/api";

type HealthState =
  | { status: "loading" }
  | { status: "ready"; data: HealthResponse }
  | { status: "error"; message: string };

export function DashboardRoute() {
  const { user, logout } = useAuth();
  const [health, setHealth] = useState<HealthState>({ status: "loading" });

  useEffect(() => {
    const controller = new AbortController();

    getHealth(controller.signal)
      .then((data) => setHealth({ status: "ready", data }))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") {
          return;
        }
        setHealth({
          status: "error",
          message: error instanceof Error ? error.message : "Unable to reach backend",
        });
      });

    return () => controller.abort();
  }, []);

  const shellStatus = useMemo(() => {
    if (health.status === "loading") {
      return { label: "Checking API", tone: "loading" as const };
    }

    if (health.status === "error") {
      return { label: "API offline", tone: "error" as const };
    }

    return health.data.status === "ok"
      ? { label: "API connected", tone: "ok" as const }
      : { label: "API degraded", tone: "degraded" as const };
  }, [health]);

  return (
    <DashboardShell
      healthLabel={shellStatus.label}
      healthTone={shellStatus.tone}
      user={user}
      onLogout={logout}
    >
      <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_280px]">
        <div className="rounded-md border border-line bg-[#fffdf8] p-5 shadow-soft">
          <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
            <div>
              <p className="text-sm font-medium uppercase tracking-[0.16em] text-ink/55">
                Overview
              </p>
              <h3 className="mt-1 text-2xl font-semibold">Project foundation</h3>
            </div>
            <span className="rounded-md border border-line bg-white px-3 py-2 text-sm text-ink/70">
              MVP scaffold
            </span>
          </div>

          {health.status === "loading" && <LoadingState />}
          {health.status === "error" && <ErrorState message={health.message} />}
          {health.status === "ready" && <ReadyState health={health.data} />}
        </div>

        <div className="rounded-md border border-line bg-[#fffdf8] p-5 shadow-soft">
          <h3 className="text-base font-semibold">Next foundation checks</h3>
          <ul className="mt-4 space-y-3 text-sm text-ink/72">
            <li className="flex items-center justify-between gap-3 border-b border-line pb-3">
              <span>Backend health</span>
              <span>{health.status === "ready" ? health.data.status : "pending"}</span>
            </li>
            <li className="flex items-center justify-between gap-3 border-b border-line pb-3">
              <span>Database</span>
              <span>{health.status === "ready" ? health.data.database : "pending"}</span>
            </li>
            <li className="flex items-center justify-between gap-3">
              <span>Redis</span>
              <span>{health.status === "ready" ? health.data.redis : "pending"}</span>
            </li>
          </ul>
        </div>
      </section>
    </DashboardShell>
  );
}

function LoadingState() {
  return (
    <div className="rounded-md border border-dashed border-line bg-white p-6 text-ink/72">
      Checking backend health...
    </div>
  );
}

function ErrorState({ message }: { message: string }) {
  return (
    <div className="rounded-md border border-danger/30 bg-[#fff5f2] p-6 text-danger">
      {message}
    </div>
  );
}

function ReadyState({ health }: { health: HealthResponse }) {
  const hasWorkspace = false;

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-3">
        <Metric label="API" value={health.status} />
        <Metric label="PostgreSQL" value={health.database} />
        <Metric label="Redis" value={health.redis} />
      </div>

      {!hasWorkspace && (
        <div className="rounded-md border border-dashed border-line bg-white p-6 text-ink/72">
          No workspace has been created yet.
        </div>
      )}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-line bg-white p-4">
      <p className="text-sm text-ink/60">{label}</p>
      <p className="mt-2 text-xl font-semibold capitalize">{value}</p>
    </div>
  );
}
