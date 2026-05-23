import {
  type FormEvent,
  type ReactNode,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Bot,
  Code2,
  Folder,
  GitBranch,
  LayoutDashboard,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
  Terminal,
  Workflow,
} from "lucide-react";

import { navigate } from "../lib/router";
import {
  ApiError,
  createAgentRun,
  getAgentRun,
  getAgentStreamUrl,
  listAgentRuns,
  stopAgentRun,
  type AgentEventResponse,
  type AgentRunResponse,
  type AgentRunStatus,
  type UserResponse,
  type WorkspaceResponse,
  type WorkspaceStatus,
} from "../lib/api";

type DashboardShellProps = {
  healthLabel: string;
  healthTone: "loading" | "ok" | "degraded" | "error";
  workspace: WorkspaceResponse | null;
  workspaceStatus: WorkspaceStatus | "none" | "loading";
  user: UserResponse | null;
  onLogout: () => Promise<void>;
  children: ReactNode;
};

const navItems = [
  { label: "Dashboard", icon: LayoutDashboard },
  { label: "Workspaces", icon: Folder },
  { label: "Repositories", icon: GitBranch },
  { label: "Agents", icon: Bot },
  { label: "Editor", icon: Code2 },
  { label: "Terminal", icon: Terminal },
];
const leftSidebarStorageKey = "execute-dashboard-left-sidebar-collapsed";
const rightSidebarStorageKey = "execute-dashboard-right-sidebar-collapsed";

const gridColumnClass = {
  "false:false":
    "grid min-h-screen grid-cols-1 lg:grid-cols-[240px_minmax(0,1fr)_320px]",
  "true:false":
    "grid min-h-screen grid-cols-1 lg:grid-cols-[72px_minmax(0,1fr)_320px]",
  "false:true":
    "grid min-h-screen grid-cols-1 lg:grid-cols-[240px_minmax(0,1fr)_72px]",
  "true:true":
    "grid min-h-screen grid-cols-1 lg:grid-cols-[72px_minmax(0,1fr)_72px]",
};

function initialCollapsedState(storageKey: string) {
  try {
    return window.localStorage?.getItem(storageKey) === "true";
  } catch {
    return false;
  }
}

export function DashboardShell({
  healthLabel,
  healthTone,
  workspace,
  workspaceStatus,
  user,
  onLogout,
  children,
}: DashboardShellProps) {
  const [leftSidebarCollapsed, setLeftSidebarCollapsed] = useState(() =>
    initialCollapsedState(leftSidebarStorageKey),
  );
  const [rightSidebarCollapsed, setRightSidebarCollapsed] = useState(() =>
    initialCollapsedState(rightSidebarStorageKey),
  );

  useEffect(() => {
    try {
      window.localStorage?.setItem(
        leftSidebarStorageKey,
        String(leftSidebarCollapsed),
      );
    } catch {
      return;
    }
  }, [leftSidebarCollapsed]);

  useEffect(() => {
    try {
      window.localStorage?.setItem(
        rightSidebarStorageKey,
        String(rightSidebarCollapsed),
      );
    } catch {
      return;
    }
  }, [rightSidebarCollapsed]);

  async function handleLogout() {
    await onLogout();
    navigate("/login");
  }

  const layoutClass =
    gridColumnClass[
      `${leftSidebarCollapsed}:${rightSidebarCollapsed}` as keyof typeof gridColumnClass
    ];

  return (
    <div className="min-h-screen bg-[#ece7dc] text-ink">
      <div className={layoutClass}>
        <aside className="border-b border-line bg-ink px-4 py-5 text-white lg:border-b-0 lg:border-r">
          <div className="mb-8 flex items-start justify-between gap-2">
            <div className="flex min-w-0 items-center gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-white text-ink">
                <Workflow className="h-5 w-5" aria-hidden="true" />
              </div>
              {!leftSidebarCollapsed && (
                <div className="min-w-0">
                  <p className="text-sm uppercase tracking-[0.18em] text-white/55">
                    Execute
                  </p>
                  <h1 className="mt-2 text-2xl font-semibold">Dashboard</h1>
                </div>
              )}
            </div>
            <button
              aria-label={
                leftSidebarCollapsed
                  ? "Expand left sidebar"
                  : "Collapse left sidebar"
              }
              className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-white/15 text-white/75 transition hover:bg-white/10 hover:text-white"
              type="button"
              onClick={() => setLeftSidebarCollapsed((current) => !current)}
            >
              {leftSidebarCollapsed ? (
                <PanelLeftOpen className="h-4 w-4" aria-hidden="true" />
              ) : (
                <PanelLeftClose className="h-4 w-4" aria-hidden="true" />
              )}
            </button>
          </div>
          <nav
            className={`flex gap-2 overflow-x-auto lg:flex-col lg:overflow-visible ${
              leftSidebarCollapsed ? "items-center" : ""
            }`}
          >
            {navItems.map(({ label, icon: Icon }) => (
              <a
                key={label}
                aria-label={leftSidebarCollapsed ? label : undefined}
                href="/"
                title={leftSidebarCollapsed ? label : undefined}
                className={`flex items-center gap-3 whitespace-nowrap rounded-md text-sm transition ${
                  leftSidebarCollapsed
                    ? "h-10 w-10 justify-center px-0 py-0"
                    : "px-3 py-2"
                } ${
                  label === "Dashboard"
                    ? "bg-white text-ink"
                    : "text-white/72 hover:bg-white/10 hover:text-white"
                }`}
              >
                <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                {!leftSidebarCollapsed && <span>{label}</span>}
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
              <WorkspaceStatusPill status={workspaceStatus} />
              {user && <UserMenu user={user} onLogout={handleLogout} />}
            </div>
          </header>

          <main className="flex-1 px-5 py-5">{children}</main>

          <footer className="border-t border-line bg-panel px-5 py-3 text-sm text-ink/70">
            API: {healthLabel} | Workspace: {workspaceStatus}
          </footer>
        </div>

        <aside className="border-t border-line bg-[#fffdf8] px-4 py-5 lg:border-l lg:border-t-0">
          <div className="mb-4 flex items-center justify-between gap-2">
            <div className="flex min-w-0 items-center gap-3">
              {!rightSidebarCollapsed && (
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-line bg-white text-ink">
                  <Bot className="h-5 w-5" aria-hidden="true" />
                </div>
              )}
              {!rightSidebarCollapsed && (
                <h2 className="text-base font-semibold">Agent Panel</h2>
              )}
            </div>
            <button
              aria-label={
                rightSidebarCollapsed
                  ? "Expand agent panel"
                  : "Collapse agent panel"
              }
              className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-line bg-white text-ink/70 transition hover:bg-[#f5f1e8] hover:text-ink"
              type="button"
              onClick={() => setRightSidebarCollapsed((current) => !current)}
            >
              {rightSidebarCollapsed ? (
                <PanelRightOpen className="h-4 w-4" aria-hidden="true" />
              ) : (
                <PanelRightClose className="h-4 w-4" aria-hidden="true" />
              )}
            </button>
          </div>
          {!rightSidebarCollapsed && <AgentPanel workspace={workspace} />}
        </aside>
      </div>
    </div>
  );
}

function AgentPanel({ workspace }: { workspace: WorkspaceResponse | null }) {
  const [prompt, setPrompt] = useState("");
  const [history, setHistory] = useState<AgentRunResponse[]>([]);
  const [activeRun, setActiveRun] = useState<AgentRunResponse | null>(null);
  const [events, setEvents] = useState<AgentEventResponse[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    socketRef.current?.close();
    setPrompt("");
    setActiveRun(null);
    setEvents([]);
    setError(null);

    if (!workspace) {
      setHistory([]);
      return;
    }

    const controller = new AbortController();
    listAgentRuns(workspace.id, controller.signal)
      .then((runs) => setHistory(Array.isArray(runs) ? runs : []))
      .catch((caughtError: unknown) => {
        if (
          caughtError instanceof DOMException &&
          caughtError.name === "AbortError"
        ) {
          return;
        }
        setError(
          caughtError instanceof ApiError
            ? caughtError.message
            : "Unable to load history.",
        );
      });

    return () => {
      controller.abort();
      socketRef.current?.close();
    };
  }, [workspace]);

  const output = useMemo(
    () =>
      events
        .filter(
          (event) =>
            event.event_type === "output_text" || event.event_type === "error",
        )
        .map((event) => event.content)
        .join(""),
    [events],
  );
  const toolEvents = events.filter(
    (event) =>
      event.event_type === "tool_call" || event.event_type === "tool_result",
  );
  const status = activeRun?.status ?? "idle";
  const canStop =
    activeRun?.status === "thinking" || activeRun?.status === "executing";

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!workspace || !prompt.trim()) {
      return;
    }

    setIsSubmitting(true);
    setError(null);
    setEvents([]);
    try {
      const run = await createAgentRun(workspace.id, { prompt });
      setActiveRun(run);
      setHistory((current) => [
        run,
        ...current.filter((item) => item.id !== run.id),
      ]);
      connectStream(workspace.id, run);
    } catch (caughtError) {
      setError(
        caughtError instanceof Error
          ? caughtError.message
          : "Unable to start agent.",
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleStop() {
    if (!workspace || !activeRun) {
      return;
    }
    setError(null);
    try {
      const stopped = await stopAgentRun(workspace.id, activeRun.id);
      setActiveRun(stopped);
      setHistory((current) =>
        current.map((run) => (run.id === stopped.id ? stopped : run)),
      );
      socketRef.current?.close();
    } catch (caughtError) {
      setError(
        caughtError instanceof ApiError
          ? caughtError.message
          : "Unable to stop agent.",
      );
    }
  }

  async function openHistoryRun(run: AgentRunResponse) {
    if (!workspace) {
      return;
    }
    setError(null);
    try {
      const detail = await getAgentRun(workspace.id, run.id);
      setActiveRun(detail);
      setPrompt(detail.prompt);
      setEvents(detail.events);
      if (detail.status === "thinking" || detail.status === "executing") {
        connectStream(workspace.id, detail);
      } else {
        socketRef.current?.close();
      }
    } catch (caughtError) {
      setError(
        caughtError instanceof ApiError
          ? caughtError.message
          : "Unable to open run.",
      );
    }
  }

  function rerun(run: AgentRunResponse) {
    setPrompt(run.prompt);
  }

  function connectStream(workspaceId: string, run: AgentRunResponse) {
    socketRef.current?.close();
    const socket = new window.WebSocket(getAgentStreamUrl(workspaceId, run.id));
    socketRef.current = socket;
    socket.onmessage = (message) => {
      const event = JSON.parse(message.data) as AgentEventResponse;
      setEvents((current) =>
        current.some((existing) => existing.id === event.id)
          ? current
          : [...current, event],
      );
      const nextStatus = event.metadata.status;
      if (
        nextStatus === "thinking" ||
        nextStatus === "executing" ||
        nextStatus === "completed" ||
        nextStatus === "error" ||
        nextStatus === "stopped"
      ) {
        setActiveRun((current) =>
          current ? { ...current, status: nextStatus } : current,
        );
      }
    };
    socket.onerror = () => setError("Agent stream disconnected.");
  }

  return (
    <div className="flex h-full flex-col gap-4 text-sm">
      <AgentStatusPill status={status} />

      <div className="rounded-md border border-line bg-white p-3">
        <p className="font-medium text-ink">Current workspace</p>
        <p className="mt-1 text-ink/72">
          {workspace ? workspace.name : "No workspace selected"}
        </p>
        {workspace && (
          <p className="mt-1 truncate text-xs text-ink/55">{workspace.slug}</p>
        )}
      </div>

      <form className="space-y-3" onSubmit={handleSubmit}>
        <label className="block font-medium">
          Prompt
          <textarea
            className="mt-2 min-h-32 w-full resize-y rounded-md border border-line bg-white px-3 py-2 text-sm outline-none transition focus:border-ink"
            value={prompt}
            disabled={!workspace}
            onChange={(event) => setPrompt(event.target.value)}
          />
        </label>
        <div className="grid grid-cols-2 gap-2">
          <button
            className="rounded-md bg-ink px-3 py-2 font-semibold text-white transition hover:bg-ink/90 disabled:cursor-not-allowed disabled:bg-ink/45"
            type="submit"
            disabled={!workspace || isSubmitting || !prompt.trim() || canStop}
          >
            {isSubmitting ? "Starting..." : "Run"}
          </button>
          <button
            className="rounded-md border border-line bg-white px-3 py-2 font-semibold transition hover:bg-[#f5f1e8] disabled:cursor-not-allowed disabled:opacity-50"
            type="button"
            disabled={!canStop}
            onClick={() => void handleStop()}
          >
            Stop
          </button>
        </div>
      </form>

      {error && (
        <div className="rounded-md border border-danger/30 bg-[#fff5f2] p-3 text-danger">
          {error}
        </div>
      )}

      <div className="min-h-40 rounded-md border border-line bg-white">
        <div className="border-b border-line px-3 py-2 font-semibold">
          Output
        </div>
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words p-3 font-mono text-xs leading-5 text-ink/78">
          {output || "Agent output will appear here."}
        </pre>
      </div>

      <div className="space-y-2">
        <h3 className="font-semibold">Tools</h3>
        {toolEvents.length === 0 && (
          <div className="rounded-md border border-dashed border-line bg-white p-3 text-ink/60">
            No tool activity yet.
          </div>
        )}
        {toolEvents.map((event) => (
          <details
            className="rounded-md border border-line bg-white p-3"
            key={event.id}
          >
            <summary className="cursor-pointer font-medium">
              {event.metadata.tool
                ? String(event.metadata.tool)
                : event.event_type}
            </summary>
            <pre className="mt-2 max-h-36 overflow-auto whitespace-pre-wrap break-words font-mono text-xs text-ink/70">
              {event.content}
            </pre>
          </details>
        ))}
      </div>

      <div className="space-y-2">
        <h3 className="font-semibold">History</h3>
        {history.length === 0 && (
          <div className="rounded-md border border-dashed border-line bg-white p-3 text-ink/60">
            Previous prompts will appear here.
          </div>
        )}
        {history.map((run) => (
          <div
            className="rounded-md border border-line bg-white p-3"
            key={run.id}
          >
            <button
              className="line-clamp-2 w-full text-left font-medium text-ink"
              type="button"
              onClick={() => void openHistoryRun(run)}
            >
              {run.prompt}
            </button>
            <div className="mt-3 flex items-center justify-between gap-2">
              <span className="capitalize text-ink/55">{run.status}</span>
              <button
                className="rounded-md border border-line px-2 py-1 text-xs font-semibold transition hover:bg-[#f5f1e8]"
                type="button"
                onClick={() => rerun(run)}
              >
                Rerun
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function AgentStatusPill({ status }: { status: AgentRunStatus | "idle" }) {
  const toneClass = {
    idle: "bg-[#e3efe9] text-signal",
    thinking: "bg-[#e8edf2] text-ink",
    executing: "bg-[#f7eadc] text-warn",
    completed: "bg-[#e3efe9] text-signal",
    error: "bg-[#fde7e3] text-danger",
    stopped: "bg-white text-ink/60",
  }[status];

  return (
    <span
      className={`rounded-full px-2 py-1 text-xs font-medium capitalize ${toneClass}`}
    >
      {status}
    </span>
  );
}

function UserMenu({
  user,
  onLogout,
}: {
  user: UserResponse;
  onLogout: () => Promise<void>;
}) {
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

function StatusPill({
  label,
  tone,
}: {
  label: string;
  tone: DashboardShellProps["healthTone"];
}) {
  const toneClass = {
    loading: "bg-[#e8edf2] text-ink",
    ok: "bg-[#e3efe9] text-signal",
    degraded: "bg-[#f7eadc] text-warn",
    error: "bg-[#fde7e3] text-danger",
  }[tone];

  return (
    <span
      className={`rounded-full px-3 py-1 text-sm font-semibold ${toneClass}`}
    >
      {label}
    </span>
  );
}

function WorkspaceStatusPill({
  status,
}: {
  status: DashboardShellProps["workspaceStatus"];
}) {
  const toneClass = {
    loading: "bg-[#e8edf2] text-ink",
    none: "bg-white text-ink/60",
    creating: "bg-[#e8edf2] text-ink",
    ready: "bg-[#e3efe9] text-signal",
    busy: "bg-[#f7eadc] text-warn",
    error: "bg-[#fde7e3] text-danger",
    stopped: "bg-white text-ink/60",
  }[status];

  const label = status === "none" ? "No workspace" : `Workspace ${status}`;
  return (
    <span
      className={`rounded-full px-3 py-1 text-sm font-semibold ${toneClass}`}
    >
      {label}
    </span>
  );
}
