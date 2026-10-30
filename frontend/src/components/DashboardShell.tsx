import {
  type ReactNode,
  useEffect,
  useState,
} from "react";
import { Bell, CheckCheck } from "lucide-react";
import { NavLink, useNavigate } from "react-router";

import { AppSidebar } from "./AppSidebar";
import { PresenceAvatars } from "./PresenceAvatars";
import {
  getNotificationStreamUrl,
  isServerReady,
  listNotifications,
  markAllNotificationsRead,
  markNotificationRead,
  type NotificationResponse,
  type UserResponse,
  type WorkspaceResponse,
  type WorkspaceStatus,
} from "../lib/api";
import { useOnlineStatus } from "../lib/useOnlineStatus";
import {
  useReconnectingWebSocket,
  type StreamConnectionState,
} from "../lib/useReconnectingWebSocket";

type DashboardShellProps = {
  healthLabel: string;
  healthTone: "loading" | "ok" | "degraded" | "error";
  workspace: WorkspaceResponse | null;
  workspaceStatus: WorkspaceStatus | "none" | "loading";
  previewStatusLabel: string;
  user: UserResponse | null;
  onLogout: () => Promise<void>;
  children: ReactNode;
  basePath?: string;
  projectId?: string;
  teamId?: string;
};

function formatDateTime(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export function DashboardShell({
  healthLabel,
  healthTone,
  workspace,
  workspaceStatus,
  previewStatusLabel,
  user,
  onLogout,
  children,
  basePath = "/",
  projectId,
  teamId,
}: DashboardShellProps) {
  const navigate = useNavigate();
  const online = useOnlineStatus();
  const [streamState, setStreamState] = useState<StreamConnectionState>("idle");

  async function handleLogout() {
    await onLogout();
    navigate("/login", { replace: true });
  }

  return (
    <div className="min-h-screen bg-canvas text-ink">
      <div className="flex min-h-screen flex-col lg:flex-row">
        <AppSidebar
          projectId={projectId}
          teamId={teamId}
          workspaceBasePath={basePath}
          showUser={false}
        />

        <div className="flex min-h-screen min-w-0 flex-1 flex-col">
          <ConnectionBanner online={online} streamState={streamState} />
          <header className="flex min-h-16 flex-wrap items-center justify-between gap-3 border-b border-line bg-canvas px-5 py-3">
            <div>
              {projectId && teamId ? (
                <nav className="flex items-center gap-1 text-xs text-muted">
                  <NavLink to="/projects" className="hover:text-ink">Projects</NavLink>
                  <span>/</span>
                  <NavLink to={`/projects/${projectId}`} className="hover:text-ink">Project</NavLink>
                  <span>/</span>
                  <NavLink to={`/projects/${projectId}/teams/${teamId}`} className="hover:text-ink">Team</NavLink>
                  <span>/</span>
                  <span className="font-medium text-ink">Workspace</span>
                </nav>
              ) : (
                <p className="text-xs font-semibold uppercase tracking-[0.16em] text-muted">
                  Enigma
                </p>
              )}
              <h2 className="text-2xl font-normal tracking-[-0.02em]">
                AI Agent Control Room
              </h2>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <StatusPill label={healthLabel} tone={healthTone} />
              <WorkspaceStatusPill status={workspaceStatus} runtime={workspace?.runtime} />
              {user && workspace && <PresenceAvatars workspaceId={workspace.id} />}
              {user && <NotificationBell onConnectionStateChange={setStreamState} />}
              {user && <UserMenu user={user} onLogout={handleLogout} />}
            </div>
          </header>

          <main className="flex-1 px-5 py-5">{children}</main>

          <footer className="border-t border-line bg-canvas px-5 py-3 text-sm text-body">
            API: {healthLabel} | Workspace: {workspaceStatus} | Preview:{" "}
            {previewStatusLabel}
          </footer>
        </div>
      </div>
    </div>
  );
}

function ConnectionBanner({
  online,
  streamState,
}: {
  online: boolean;
  streamState: StreamConnectionState;
}) {
  if (!online) {
    return (
      <div
        role="status"
        className="bg-[#fff5f2] px-5 py-2 text-center text-sm font-medium text-danger"
      >
        You are offline. Live updates will resume when your connection returns.
      </div>
    );
  }
  if (streamState === "reconnecting" || streamState === "disconnected") {
    return (
      <div
        role="status"
        className="bg-[#fff8f3] px-5 py-2 text-center text-sm font-medium text-warn"
      >
        Reconnecting to the server…
      </div>
    );
  }
  return null;
}

/** Union by id, newest first. A copy marked read wins, so a refetch that raced a mark-read
 * cannot flip a notification back to unread. */
function mergeNotifications(
  fetched: NotificationResponse[],
  current: NotificationResponse[],
): NotificationResponse[] {
  const byId = new Map<string, NotificationResponse>();
  for (const item of [...current, ...fetched]) {
    const existing = byId.get(item.id);
    byId.set(item.id, existing?.read_at && !item.read_at ? existing : item);
  }
  return [...byId.values()].sort(
    (a, b) => Date.parse(b.created_at) - Date.parse(a.created_at),
  );
}

export function NotificationBell({
  onConnectionStateChange,
}: {
  onConnectionStateChange?: (state: StreamConnectionState) => void;
}) {
  const [notifications, setNotifications] = useState<NotificationResponse[]>(
    [],
  );
  const [isOpen, setIsOpen] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    // Merge rather than replace: the stream may already have delivered newer items.
    listNotifications(controller.signal)
      .then((data) => {
        if (Array.isArray(data)) {
          setNotifications((current) => mergeNotifications(data, current));
        }
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  // Long-lived notification stream: reconnect indefinitely so a transient drop recovers
  // without a page reload.
  const { connectionState } = useReconnectingWebSocket(getNotificationStreamUrl(), {
    enabled: typeof WebSocket !== "undefined",
    maxRetries: Infinity,
    // During a deploy this stays "reconnecting" (the banner) until the new instance is ready.
    readinessCheck: isServerReady,
    onOpen: () => {
      // The stream only pushes what is created while it is connected, so every open (a
      // reconnect possibly lands on another replica) backfills the gap from the list endpoint.
      listNotifications()
        .then((data) => {
          if (Array.isArray(data)) {
            setNotifications((current) => mergeNotifications(data, current));
          }
        })
        .catch(() => undefined);
    },
    onMessage: (event) => {
      try {
        const notification = JSON.parse(event.data) as NotificationResponse;
        setNotifications((current) => [
          notification,
          ...current.filter((item) => item.id !== notification.id),
        ]);
      } catch {
        return;
      }
    },
  });

  useEffect(() => {
    onConnectionStateChange?.(connectionState);
  }, [connectionState, onConnectionStateChange]);

  const unreadCount = notifications.filter(
    (notification) => !notification.read_at,
  ).length;

  async function handleMarkRead(notificationId: string) {
    const updated = await markNotificationRead(notificationId);
    setNotifications((current) =>
      current.map((notification) =>
        notification.id === updated.id ? updated : notification,
      ),
    );
  }

  async function handleMarkAllRead() {
    const updated = await markAllNotificationsRead();
    setNotifications(Array.isArray(updated) ? updated : []);
  }

  return (
    <div className="relative">
      <button
        aria-label="Notifications"
        className="relative flex h-10 w-10 items-center justify-center rounded-md border border-line bg-white text-ink/70 transition hover:bg-panel hover:text-ink"
        type="button"
        onClick={() => setIsOpen((current) => !current)}
      >
        <Bell className="h-4 w-4" aria-hidden="true" />
        {unreadCount > 0 && (
          <span className="absolute -right-1 -top-1 flex min-h-5 min-w-5 items-center justify-center rounded-full bg-danger px-1 text-xs font-semibold text-white">
            {unreadCount > 9 ? "9+" : unreadCount}
          </span>
        )}
      </button>

      {isOpen && (
        <div className="absolute right-0 top-12 z-20 w-[min(360px,calc(100vw-2rem))] rounded-md border border-line bg-white shadow-soft">
          <div className="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
            <div>
              <h3 className="text-sm font-semibold">Notifications</h3>
              <p className="text-xs text-muted">{unreadCount} unread</p>
            </div>
            <button
              aria-label="Mark all notifications read"
              className="flex h-9 w-9 items-center justify-center rounded-md border border-line bg-white text-ink/70 transition hover:bg-panel hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
              type="button"
              disabled={unreadCount === 0}
              onClick={() => void handleMarkAllRead()}
            >
              <CheckCheck className="h-4 w-4" aria-hidden="true" />
            </button>
          </div>
          <div className="max-h-96 overflow-y-auto">
            {notifications.length === 0 && (
              <div className="px-4 py-6 text-sm text-muted">
                No notifications yet.
              </div>
            )}
            {notifications.map((notification) => (
              <button
                key={notification.id}
                className={`block w-full border-b border-line px-4 py-3 text-left transition last:border-b-0 hover:bg-panel ${
                  notification.read_at ? "bg-white" : "bg-[#fff8f3]"
                }`}
                type="button"
                onClick={() => void handleMarkRead(notification.id)}
              >
                <div className="flex items-start justify-between gap-3">
                  <p className="text-sm font-semibold text-ink">
                    {notification.title}
                  </p>
                  {!notification.read_at && (
                    <span className="mt-1 h-2 w-2 shrink-0 rounded-full bg-primary" />
                  )}
                </div>
                <p className="mt-1 text-sm text-body">{notification.message}</p>
                <p className="mt-2 text-xs text-muted">
                  {formatDateTime(notification.created_at)}
                </p>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
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
        className="rounded-md border border-line px-3 py-1 text-sm font-medium transition hover:bg-panel"
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
    loading: "bg-white text-muted ring-1 ring-line",
    ok: "bg-white text-signal ring-1 ring-line",
    degraded: "bg-white text-warn ring-1 ring-line",
    error: "bg-[#fff5f2] text-danger",
  }[tone];

  return (
    <span
      className={`rounded-full px-3 py-1 text-sm font-semibold ${toneClass}`}
    >
      {label}
    </span>
  );
}

export function WorkspaceStatusPill({
  status,
  runtime,
}: {
  status: DashboardShellProps["workspaceStatus"];
  runtime?: string;
}) {
  const toneClass = {
    loading: "bg-white text-muted ring-1 ring-line",
    none: "bg-white text-muted ring-1 ring-line",
    creating: "bg-white text-ink ring-1 ring-line",
    ready: "bg-white text-signal ring-1 ring-line",
    busy: "bg-white text-warn ring-1 ring-line",
    error: "bg-[#fff5f2] text-danger",
    stopped: "bg-white text-muted ring-1 ring-line",
  }[status];

  const label = status === "none" ? "No workspace" : `Workspace ${status}`;
  // Idle Docker sandboxes are stopped, not deleted; the next use starts them again.
  const wakesOnUse = status === "stopped" && runtime === "docker";
  return (
    <span
      className={`rounded-full px-3 py-1 text-sm font-semibold ${toneClass}`}
    >
      {label}
      {wakesOnUse && (
        <span className="ml-1 font-normal text-muted"> · wakes on next use</span>
      )}
    </span>
  );
}
