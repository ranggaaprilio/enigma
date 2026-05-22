import type { ReactNode } from "react";

import { navigate } from "../lib/router";
import type { UserResponse } from "../lib/api";

type DashboardShellProps = {
  healthLabel: string;
  healthTone: "loading" | "ok" | "degraded" | "error";
  user: UserResponse | null;
  onLogout: () => Promise<void>;
  children: ReactNode;
};

const navItems = ["Dashboard", "Workspaces", "Repositories", "Agents", "Terminal"];

export function DashboardShell({
  healthLabel,
  healthTone,
  user,
  onLogout,
  children,
}: DashboardShellProps) {
  async function handleLogout() {
    await onLogout();
    navigate("/login");
  }

  return (
    <div className="min-h-screen bg-[#ece7dc] text-ink">
      <div className="grid min-h-screen grid-cols-1 lg:grid-cols-[240px_minmax(0,1fr)_320px]">
        <aside className="border-b border-line bg-ink px-5 py-5 text-white lg:border-b-0 lg:border-r">
          <div className="mb-8">
            <p className="text-sm uppercase tracking-[0.18em] text-white/55">Execute</p>
            <h1 className="mt-2 text-2xl font-semibold">Dashboard</h1>
          </div>
          <nav className="flex gap-2 overflow-x-auto lg:flex-col lg:overflow-visible">
            {navItems.map((item) => (
              <a
                key={item}
                href="/"
                className={`whitespace-nowrap rounded-md px-3 py-2 text-sm transition ${
                  item === "Dashboard"
                    ? "bg-white text-ink"
                    : "text-white/72 hover:bg-white/10 hover:text-white"
                }`}
              >
                {item}
              </a>
            ))}
          </nav>
        </aside>

        <div className="flex min-h-screen min-w-0 flex-col">
          <header className="flex min-h-16 flex-wrap items-center justify-between gap-3 border-b border-line bg-panel px-5 py-3">
            <div>
              <p className="text-xs font-medium uppercase tracking-[0.16em] text-ink/55">
                Local foundation
              </p>
              <h2 className="text-xl font-semibold">AI Agent Control Room</h2>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <StatusPill label={healthLabel} tone={healthTone} />
              {user && <UserMenu user={user} onLogout={handleLogout} />}
            </div>
          </header>

          <main className="flex-1 px-5 py-5">{children}</main>

          <footer className="border-t border-line bg-panel px-5 py-3 text-sm text-ink/70">
            API: {healthLabel}
          </footer>
        </div>

        <aside className="border-t border-line bg-[#fffdf8] px-5 py-5 lg:border-l lg:border-t-0">
          <div className="mb-5 flex items-center justify-between">
            <h2 className="text-base font-semibold">Agent Panel</h2>
            <span className="rounded-full bg-[#e3efe9] px-2 py-1 text-xs font-medium text-signal">
              Idle
            </span>
          </div>
          <div className="space-y-3 text-sm text-ink/72">
            <div className="rounded-md border border-line bg-white p-3">
              <p className="font-medium text-ink">Current workspace</p>
              <p className="mt-1">No workspace selected</p>
            </div>
            <div className="rounded-md border border-line bg-white p-3">
              <p className="font-medium text-ink">Session output</p>
              <p className="mt-1">Ready for the next epic.</p>
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}

function UserMenu({ user, onLogout }: { user: UserResponse; onLogout: () => Promise<void> }) {
  const displayName = user.name ?? user.email;

  return (
    <div className="flex items-center gap-3 rounded-md border border-line bg-white px-3 py-2">
      <div className="min-w-0">
        <p className="max-w-44 truncate text-sm font-semibold">{displayName}</p>
        <p className="text-xs uppercase text-ink/55">{user.role}</p>
      </div>
      <button
        className="rounded-md border border-line px-3 py-1 text-sm font-medium transition hover:bg-[#f5f1e8]"
        type="button"
        onClick={() => void onLogout()}
      >
        Logout
      </button>
    </div>
  );
}

function StatusPill({ label, tone }: { label: string; tone: DashboardShellProps["healthTone"] }) {
  const toneClass = {
    loading: "bg-[#e8edf2] text-ink",
    ok: "bg-[#e3efe9] text-signal",
    degraded: "bg-[#f7eadc] text-warn",
    error: "bg-[#fde7e3] text-danger",
  }[tone];

  return (
    <span className={`rounded-full px-3 py-1 text-sm font-semibold ${toneClass}`}>{label}</span>
  );
}
