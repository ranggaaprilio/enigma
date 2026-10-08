import { type FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Pencil,
  Play,
  Plus,
  RotateCcw,
  TerminalSquare,
  Trash2,
  XCircle,
} from "lucide-react";

import {
  ApiError,
  createCustomCommand,
  deleteCustomCommand,
  getCommandRunStreamUrl,
  interruptionMessage,
  isServerReady,
  listCustomCommandRuns,
  listCustomCommands,
  runCustomCommand,
  updateCustomCommand,
  reorderCustomCommands,
  updateWorkspace,
  type CommandRole,
  type CommandRunResponse,
  type CommandRunStreamMessage,
  type CustomCommandPayload,
  type CustomCommandResponse,
  type WorkspaceResponse,
} from "../lib/api";
import { useReconnectingWebSocket } from "../lib/useReconnectingWebSocket";
import { usePermissions } from "../auth/permissions";
import { useActiveWorktree } from "../lib/activeWorktree";
import { RunConfigurationTab } from "./RunConfigurationTab";
import { hotkeyMatches, normalizeHotkey } from "../lib/hotkeys";
import { StatusPill } from "./ui/StatusPill";

type ModalMode = "create" | "edit";
type CommandFormState = {
  name: string;
  command: string;
  description: string;
  icon: string;
  hotkey: string;
  requiresConfirmation: boolean;
  role: CommandRole;
  // Whether this runs as a long-lived service (Epic 11) instead of a one-shot task.
  isService: boolean;
  cwd: string;
  port: string;
  timeoutSeconds: string;
  enabled: boolean;
  // Epic 13: readiness + "Open app" entry point -- only meaningful for a service.
  healthPath: string;
  isEntryPoint: boolean;
};

const emptyForm: CommandFormState = {
  name: "",
  command: "",
  description: "",
  icon: "",
  hotkey: "",
  requiresConfirmation: false,
  role: "custom",
  isService: false,
  cwd: "",
  port: "",
  timeoutSeconds: "",
  enabled: true,
  healthPath: "",
  isEntryPoint: false,
};

/** Registry entries are shown grouped by lifecycle, in the order you would actually run them. */
const ROLE_ORDER: CommandRole[] = [
  "install",
  "migrate",
  "seed",
  "dev",
  "test",
  "lint",
  "custom",
];

const ROLE_LABEL: Record<CommandRole, string> = {
  install: "Install",
  migrate: "Migrate",
  seed: "Seed",
  dev: "Run",
  test: "Test",
  lint: "Lint",
  custom: "Other",
};

export function CustomCommandsPanel({ workspace }: { workspace: WorkspaceResponse }) {
  const [commands, setCommands] = useState<CustomCommandResponse[]>([]);
  const [selectedCommandId, setSelectedCommandId] = useState<string | null>(null);
  const [runs, setRuns] = useState<CommandRunResponse[]>([]);
  const [activeRun, setActiveRun] = useState<CommandRunResponse | null>(null);
  const [streamOutput, setStreamOutput] = useState("");
  const [modalMode, setModalMode] = useState<ModalMode | null>(null);
  const [formState, setFormState] = useState<CommandFormState>(emptyForm);
  const [pendingConfirmation, setPendingConfirmation] =
    useState<CustomCommandResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [isRunning, setIsRunning] = useState(false);
  // Non-null while a freshly-started run should be streamed; drives the reconnecting socket.
  const [streamingRunId, setStreamingRunId] = useState<string | null>(null);
  const stopStreamRef = useRef<() => void>(() => {});
  // Read inside stream callbacks (which are not re-created per render) to tell whether the
  // run being streamed is still the one on screen.
  const activeRunIdRef = useRef<string | null>(null);
  activeRunIdRef.current = activeRun?.id ?? null;
  // Lets the hotkey listener call the latest `requestRun` without re-binding on every render.
  const requestRunRef = useRef<(command: CustomCommandResponse) => void>(() => {});

  const [tab, setTab] = useState<"registry" | "configuration">("registry");
  const [activeWorktree] = useActiveWorktree(workspace.id);
  const [appRoot, setAppRoot] = useState(workspace.app_root ?? "");

  const { can } = usePermissions();
  const canRunCommand = can("command.run");
  const canManageCommand = can("command.manage");

  // Grouped by lifecycle so `install` → `migrate` → `dev` reads in the order you run them,
  // and ordered by the user's own sort_order inside each group.
  const groupedCommands = useMemo(() => {
    const ordered = [...commands].sort(
      (a, b) => a.sort_order - b.sort_order || a.created_at.localeCompare(b.created_at),
    );
    return ROLE_ORDER.map((role) => ({
      role,
      items: ordered.filter((command) => command.role === role),
    })).filter((group) => group.items.length > 0);
  }, [commands]);

  const selectedCommand = useMemo(
    () => commands.find((command) => command.id === selectedCommandId) ?? null,
    [commands, selectedCommandId],
  );
  // The live buffer belongs to `streamingRunId` alone. Keying off `status === "running"`
  // instead showed it under any selected run that happened to be running (an orphan from a
  // previous session, say), attributing one run's output to another.
  const visibleOutput =
    activeRun && activeRun.id === streamingRunId ? streamOutput : activeRun?.output ?? "";

  useEffect(() => {
    setStreamingRunId(null);
    setCommands([]);
    setRuns([]);
    setActiveRun(null);
    setStreamOutput("");
    setError(null);

    const controller = new AbortController();
    listCustomCommands(workspace.id, controller.signal)
      .then((items) => {
        setCommands(items);
        setSelectedCommandId(items[0]?.id ?? null);
      })
      .catch((caughtError: unknown) => {
        if (caughtError instanceof DOMException && caughtError.name === "AbortError") {
          return;
        }
        setError(
          caughtError instanceof ApiError ? caughtError.message : "Unable to load commands.",
        );
      });

    return () => {
      controller.abort();
    };
  }, [workspace.id]);

  // Keyed on `selectedCommandId`, not `selectedCommand`: the latter is re-derived by `useMemo`
  // from `commands`, so it gets a new object identity every time `commands` is replaced (a
  // reorder, an edit, importing presets) even when the selected command itself hasn't changed.
  // Depending on the object tore down a live run stream — `setStreamingRunId(null)` below — on
  // every such unrelated update.
  useEffect(() => {
    setStreamingRunId(null);
    setRuns([]);
    setActiveRun(null);
    setStreamOutput("");
    if (!selectedCommandId) {
      return;
    }

    const controller = new AbortController();
    listCustomCommandRuns(workspace.id, selectedCommandId, controller.signal)
      .then((items) => {
        setRuns(items);
        setActiveRun(items[0] ?? null);
      })
      .catch((caughtError: unknown) => {
        if (caughtError instanceof DOMException && caughtError.name === "AbortError") {
          return;
        }
        setError(
          caughtError instanceof ApiError ? caughtError.message : "Unable to load command runs.",
        );
      });

    return () => controller.abort();
  }, [workspace.id, selectedCommandId]);

  function openCreateModal() {
    setFormState(emptyForm);
    setModalMode("create");
    setError(null);
  }

  function openEditModal(command: CustomCommandResponse) {
    setFormState({
      name: command.name,
      command: command.command,
      description: command.description ?? "",
      icon: command.icon ?? "",
      hotkey: command.hotkey ?? "",
      requiresConfirmation: command.requires_confirmation,
      role: command.role,
      isService: command.kind === "service",
      cwd: command.cwd ?? "",
      port: command.port === null ? "" : String(command.port),
      timeoutSeconds: command.timeout_seconds === null ? "" : String(command.timeout_seconds),
      enabled: command.enabled,
      healthPath: command.health_path ?? "",
      isEntryPoint: command.is_entry_point,
    });
    setModalMode("edit");
    setError(null);
  }

  async function saveCommand(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isSaving) {
      return;
    }
    setIsSaving(true);
    setError(null);
    const payload = formPayload(formState);
    try {
      if (modalMode === "edit" && selectedCommand) {
        const updated = await updateCustomCommand(workspace.id, selectedCommand.id, payload);
        setCommands((current) =>
          current.map((command) => (command.id === updated.id ? updated : command)),
        );
        setSelectedCommandId(updated.id);
      } else {
        const created = await createCustomCommand(workspace.id, payload);
        setCommands((current) => [...current, created]);
        setSelectedCommandId(created.id);
      }
      setModalMode(null);
      setFormState(emptyForm);
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : "Unable to save command.");
    } finally {
      setIsSaving(false);
    }
  }

  async function removeCommand(command: CustomCommandResponse) {
    setError(null);
    try {
      await deleteCustomCommand(workspace.id, command.id);
      setCommands((current) => current.filter((item) => item.id !== command.id));
      setSelectedCommandId((current) => {
        if (current !== command.id) {
          return current;
        }
        const next = commands.find((item) => item.id !== command.id);
        return next?.id ?? null;
      });
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : "Unable to delete command.");
    }
  }

  /**
   * Swap a command with its neighbour *in its role group* and persist the whole registry's new
   * order. The list is rendered grouped by role, so the neighbour in the flat sort_order list
   * may belong to another group; swapping with it would change nothing visible while still
   * rewriting every sort_order (and possibly which `dev` entry the dev server launches).
   */
  async function moveCommand(command: CustomCommandResponse, direction: -1 | 1) {
    const ordered = [...commands].sort(
      (a, b) => a.sort_order - b.sort_order || a.created_at.localeCompare(b.created_at),
    );
    const index = ordered.findIndex((item) => item.id === command.id);
    if (index < 0) {
      return;
    }
    let target = index + direction;
    while (target >= 0 && target < ordered.length && ordered[target].role !== command.role) {
      target += direction;
    }
    if (target < 0 || target >= ordered.length) {
      return;
    }
    [ordered[index], ordered[target]] = [ordered[target], ordered[index]];
    setError(null);
    try {
      const updated = await reorderCustomCommands(workspace.id, {
        items: ordered.map((item, position) => ({ id: item.id, sort_order: (position + 1) * 10 })),
      });
      setCommands(updated);
    } catch (caughtError) {
      setError(
        caughtError instanceof ApiError ? caughtError.message : "Unable to reorder commands.",
      );
    }
  }

  async function saveAppRoot(value: string) {
    setError(null);
    try {
      const updated = await updateWorkspace(workspace.id, { app_root: value.trim() || "" });
      setAppRoot(updated.app_root ?? "");
    } catch (caughtError) {
      setError(
        caughtError instanceof ApiError ? caughtError.message : "Unable to save the app root.",
      );
      throw caughtError;
    }
  }

  function requestRun(command: CustomCommandResponse) {
    if (command.requires_confirmation) {
      setPendingConfirmation(command);
      return;
    }
    void startRun(command, false);
  }
  requestRunRef.current = requestRun;

  async function startRun(command: CustomCommandResponse, confirmed: boolean) {
    setIsRunning(true);
    setError(null);
    setPendingConfirmation(null);
    setStreamOutput("");
    setStreamingRunId(null);
    try {
      const run = await runCustomCommand(workspace.id, command.id, {
        confirmed,
        // Run where the user is working, matching the terminal and agent.
        worktree_id: activeWorktree?.id ?? null,
      });
      setActiveRun(run);
      setRuns((current) => [run, ...current.filter((item) => item.id !== run.id)]);
      // Drive the reconnecting stream off state; the hook opens/closes the socket.
      setStreamingRunId(run.id);
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : "Unable to run command.");
    } finally {
      setIsRunning(false);
    }
  }

  const streamUrl = streamingRunId ? getCommandRunStreamUrl(workspace.id, streamingRunId) : null;

  // Every stream write is addressed to the run the socket belongs to. Selecting an older
  // run from the history reassigns `activeRun` while the socket stays open, so an
  // unconditional update appended live chunks to that historical run and overwrote its
  // recorded status/exit code with the live result.
  const updateStreamingRun = useCallback(
    (updater: (run: CommandRunResponse) => CommandRunResponse) => {
      setActiveRun((current) =>
        current && current.id === streamingRunId ? updater(current) : current,
      );
    },
    [streamingRunId],
  );

  const { connectionState, stopReconnect } = useReconnectingWebSocket(streamUrl, {
    readinessCheck: isServerReady,
    onBeforeConnect: () => {
      // Backend replays the run's full output on every (re)connect; reset to avoid
      // duplicates. Only ever blank the streaming run — a reconnect must not wipe the
      // output of a finished run the user has selected in the meantime.
      if (activeRunIdRef.current === streamingRunId) {
        setStreamOutput("");
      }
      updateStreamingRun((run) => ({ ...run, output: "" }));
    },
    onMessage: (message) => {
      let payload: CommandRunStreamMessage;
      try {
        payload = JSON.parse(message.data);
      } catch {
        return; // ignore malformed / keepalive frames
      }
      if (payload.type === "output") {
        if (activeRunIdRef.current === streamingRunId) {
          setStreamOutput((current) => `${current}${payload.content}`);
        }
        updateStreamingRun((run) => ({
          ...run,
          output: `${run.output}${payload.content}`,
        }));
        return;
      }
      const errorReason = payload.reason ?? null;
      updateStreamingRun((run) => ({
        ...run,
        status: payload.status,
        exit_code: payload.exit_code,
        error_reason: errorReason,
        completed_at: payload.status === "running" ? null : new Date().toISOString(),
      }));
      setRuns((current) =>
        current.map((item) =>
          item.id === streamingRunId
            ? {
                ...item,
                status: payload.status,
                exit_code: payload.exit_code,
                error_reason: errorReason,
              }
            : item,
        ),
      );
      if (payload.status !== "running") {
        // Terminal: stop reconnecting before the backend closes the socket, then detach.
        stopStreamRef.current();
        setStreamingRunId(null);
      }
    },
  });

  useEffect(() => {
    stopStreamRef.current = stopReconnect;
  }, [stopReconnect]);

  useEffect(() => {
    if (connectionState === "disconnected") {
      setError("Command stream disconnected.");
    }
  }, [connectionState]);

  // The `hotkey` field has always been stored but never bound. Ctrl/Cmd+Alt+<key> keeps
  // `install` / `migrate` / `dev` one keystroke away without stealing browser shortcuts.
  useEffect(() => {
    if (!canRunCommand) {
      return;
    }
    function onKeyDown(event: KeyboardEvent) {
      if (!(event.ctrlKey || event.metaKey) || !event.altKey) {
        return;
      }
      const match = commands.find(
        (command) => command.enabled && hotkeyMatches(command.hotkey, event),
      );
      if (!match) {
        return;
      }
      event.preventDefault();
      setSelectedCommandId(match.id);
      requestRunRef.current(match);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [commands, canRunCommand]);

  return (
    <section className="rounded-md border border-line bg-white p-5 shadow-soft">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-sm font-medium uppercase tracking-[0.16em] text-ink/55">
            Commands
          </p>
          <h3 className="mt-1 text-base font-semibold">{workspace.name}</h3>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex rounded-md border border-line bg-panel p-0.5">
            {(
              [
                ["registry", "Registry"],
                ["configuration", "Run configuration"],
              ] as const
            ).map(([id, label]) => (
              <button
                key={id}
                className={`rounded px-3 py-1.5 text-sm font-semibold capitalize transition ${
                  tab === id ? "bg-white text-ink shadow-soft" : "text-ink/60 hover:text-ink"
                }`}
                type="button"
                onClick={() => setTab(id)}
              >
                {label}
              </button>
            ))}
          </div>
          {canManageCommand && tab === "registry" && (
            <button
              className="flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-semibold text-white transition hover:bg-primary-active"
              type="button"
              onClick={openCreateModal}
            >
              <Plus className="h-4 w-4" aria-hidden="true" />
              New command
            </button>
          )}
        </div>
      </div>

      {activeWorktree && tab === "registry" && (
        <p className="mb-4 rounded-md border border-line bg-panel px-3 py-2 text-xs text-ink/70">
          Commands run on <span className="font-semibold">{activeWorktree.branch}</span>.
        </p>
      )}

      {error && (
        <div className="mb-4 rounded-md border border-danger/30 bg-[#fff5f2] p-3 text-sm text-danger">
          {error}
        </div>
      )}

      {tab === "configuration" && (
        <RunConfigurationTab
          workspace={workspace}
          appRoot={appRoot}
          canManage={canManageCommand}
          worktreeId={activeWorktree?.id ?? null}
          onSaveAppRoot={saveAppRoot}
          onCommandsImported={(created) => {
            setCommands((current) => [...current, ...created]);
            setTab("registry");
          }}
        />
      )}

      <div
        className={`grid gap-4 xl:grid-cols-[340px_minmax(0,1fr)] ${
          tab === "registry" ? "" : "hidden"
        }`}
      >
        <div className="space-y-4">
          {commands.length === 0 && (
            <div className="rounded-md border border-dashed border-line bg-white p-4 text-sm text-ink/60">
              No commands yet. Use <span className="font-semibold">Run configuration</span> to
              detect them from your repository.
            </div>
          )}
          {groupedCommands.map((group) => (
            <div key={group.role} className="space-y-2">
              <p className="text-xs font-semibold uppercase tracking-[0.16em] text-ink/45">
                {ROLE_LABEL[group.role]}
              </p>
              {group.items.map((command, position) => (
            <div
              key={command.id}
              className={`rounded-md border transition ${
                selectedCommandId === command.id
                  ? "border-ink bg-white shadow-soft"
                  : "border-line bg-white hover:border-ink/45"
              } ${command.enabled ? "" : "opacity-60"}`}
            >
              <button
                className="w-full p-4 text-left"
                type="button"
                onClick={() => setSelectedCommandId(command.id)}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <TerminalSquare
                        className="h-4 w-4 shrink-0 text-ink/55"
                        aria-hidden="true"
                      />
                      <span className="truncate font-semibold">{command.name}</span>
                      {!command.enabled && (
                        <span className="shrink-0 rounded-full bg-line px-2 py-0.5 text-xs font-semibold text-ink/60">
                          off
                        </span>
                      )}
                      {command.kind === "service" && (
                        <span
                          className="shrink-0 rounded-full bg-line px-2 py-0.5 text-xs font-semibold text-ink/60"
                          title="Runs as a service in the Preview tab"
                        >
                          service
                        </span>
                      )}
                    </div>
                    <code className="mt-2 block truncate rounded-md bg-panel px-2 py-1 text-xs text-ink/75">
                      {command.command}
                    </code>
                  </div>
                  {command.requires_confirmation && (
                    <AlertTriangle
                      className="h-4 w-4 shrink-0 text-warn"
                      aria-label="Asks before running"
                    />
                  )}
                </div>
                <div className="mt-3 flex items-center justify-between gap-2 text-xs text-ink/60">
                  <span className="truncate">
                    {command.description ||
                      [command.cwd, command.port ? `port ${command.port}` : null]
                        .filter(Boolean)
                        .join(" · ")}
                  </span>
                  {command.hotkey && (
                    <kbd
                      className="rounded border border-line bg-white px-2 py-1 font-mono"
                      title="Ctrl/Cmd + Alt + key"
                    >
                      {command.hotkey.toUpperCase()}
                    </kbd>
                  )}
                </div>
              </button>
              {canManageCommand && (
                <div className="flex justify-end gap-1 border-t border-line px-2 py-1">
                  <button
                    aria-label={`Move ${command.name} up`}
                    className="flex h-7 w-7 items-center justify-center rounded text-ink/50 transition hover:bg-panel hover:text-ink disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-transparent"
                    type="button"
                    title="Move up"
                    disabled={position === 0}
                    onClick={() => void moveCommand(command, -1)}
                  >
                    <ArrowUp className="h-3.5 w-3.5" aria-hidden="true" />
                  </button>
                  <button
                    aria-label={`Move ${command.name} down`}
                    className="flex h-7 w-7 items-center justify-center rounded text-ink/50 transition hover:bg-panel hover:text-ink disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-transparent"
                    type="button"
                    title="Move down"
                    disabled={position === group.items.length - 1}
                    onClick={() => void moveCommand(command, 1)}
                  >
                    <ArrowDown className="h-3.5 w-3.5" aria-hidden="true" />
                  </button>
                </div>
              )}
            </div>
          ))}
            </div>
          ))}
        </div>

        <div className="min-w-0 rounded-md border border-line bg-white">
          {!selectedCommand && (
            <div className="p-6 text-sm text-ink/60">Select a command to view runs.</div>
          )}
          {selectedCommand && (
            <>
              <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line p-4">
                <div className="min-w-0">
                  <h4 className="text-lg font-semibold">{selectedCommand.name}</h4>
                  <code className="mt-2 block break-words rounded-md bg-panel px-2 py-1 text-xs text-ink/75">
                    {selectedCommand.command}
                  </code>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    aria-label="Run command"
                    className="flex h-10 w-10 items-center justify-center rounded-md bg-primary text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45"
                    type="button"
                    disabled={isRunning || activeRun?.status === "running" || !canRunCommand}
                    title={canRunCommand ? "Run command" : "You do not have permission to run commands"}
                    onClick={() => requestRun(selectedCommand)}
                  >
                    <Play className="h-4 w-4" aria-hidden="true" />
                  </button>
                  {canManageCommand && (
                    <>
                      <button
                        aria-label="Edit command"
                        className="flex h-10 w-10 items-center justify-center rounded-md border border-line bg-white text-ink/70 transition hover:bg-panel hover:text-ink"
                        type="button"
                        title="Edit command"
                        onClick={() => openEditModal(selectedCommand)}
                      >
                        <Pencil className="h-4 w-4" aria-hidden="true" />
                      </button>
                      <button
                        aria-label="Delete command"
                        className="flex h-10 w-10 items-center justify-center rounded-md border border-line bg-white text-danger transition hover:bg-[#fff5f2]"
                        type="button"
                        title="Delete command"
                        onClick={() => void removeCommand(selectedCommand)}
                      >
                        <Trash2 className="h-4 w-4" aria-hidden="true" />
                      </button>
                    </>
                  )}
                </div>
              </div>

              <div className="grid gap-4 p-4 lg:grid-cols-[minmax(0,1fr)_260px]">
                <div className="min-w-0">
                  <div className="mb-3 flex items-center justify-between gap-3">
                    <h5 className="font-semibold">Output</h5>
                    <StatusPill status={activeRun?.status ?? "idle"} />
                  </div>
                  {interruptionMessage(activeRun?.error_reason) && (
                    <div
                      className="mb-3 flex items-center gap-2 rounded-md border border-warn/30 bg-[#fffbf0] px-3 py-2 text-sm text-warn"
                      data-testid="command-run-interrupted"
                      role="status"
                    >
                      <RotateCcw className="h-4 w-4" aria-hidden="true" />
                      {interruptionMessage(activeRun?.error_reason)}
                    </div>
                  )}
                  <pre className="min-h-[360px] max-h-[520px] overflow-auto whitespace-pre-wrap break-words rounded-lg border border-line bg-panel p-4 font-mono text-xs leading-5 text-ink">
                    {visibleOutput || "Run output will appear here."}
                  </pre>
                </div>

                <div className="space-y-2">
                  <h5 className="font-semibold">Run history</h5>
                  {runs.length === 0 && (
                    <div className="rounded-md border border-dashed border-line bg-white p-3 text-sm text-ink/60">
                      No runs yet.
                    </div>
                  )}
                  {runs.map((run) => (
                    <button
                      key={run.id}
                      className={`w-full rounded-md border p-3 text-left text-sm transition ${
                        activeRun?.id === run.id
                          ? "border-ink bg-white"
                          : "border-line hover:border-ink/45"
                      }`}
                      type="button"
                      // Selecting a run only changes what is displayed; the live buffer is
                      // left intact so switching back to a still-streaming run resumes it.
                      onClick={() => setActiveRun(run)}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="capitalize">{run.status}</span>
                        <span className="text-xs text-ink/55">
                          {run.exit_code === null ? "-" : run.exit_code}
                        </span>
                      </div>
                      <time className="mt-2 block text-xs text-ink/55">
                        {new Date(run.started_at).toLocaleString()}
                      </time>
                      {(run.worktree_id || run.cwd) && (
                        <div className="mt-1 flex flex-wrap gap-1 text-xs text-ink/50">
                          {run.worktree_id && (
                            <span className="rounded-full bg-line px-2 py-0.5">
                              {run.worktree_id === activeWorktree?.id
                                ? activeWorktree.branch
                                : "worktree"}
                            </span>
                          )}
                          {run.cwd && (
                            <span className="rounded-full bg-line px-2 py-0.5 font-mono">
                              {run.cwd}
                            </span>
                          )}
                        </div>
                      )}
                    </button>
                  ))}
                </div>
              </div>
            </>
          )}
        </div>
      </div>

      {modalMode && (
        <CommandModal
          mode={modalMode}
          formState={formState}
          isSaving={isSaving}
          onChange={setFormState}
          onClose={() => setModalMode(null)}
          onSubmit={saveCommand}
        />
      )}

      {pendingConfirmation && (
        <ConfirmationModal
          command={pendingConfirmation}
          onCancel={() => setPendingConfirmation(null)}
          onConfirm={() => void startRun(pendingConfirmation, true)}
        />
      )}
    </section>
  );
}

function CommandModal({
  mode,
  formState,
  isSaving,
  onChange,
  onClose,
  onSubmit,
}: {
  mode: ModalMode;
  formState: CommandFormState;
  isSaving: boolean;
  onChange: (state: CommandFormState) => void;
  onClose: () => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/45 p-4">
      <form
        className="w-full max-w-xl rounded-md border border-line bg-white p-5 shadow-soft"
        onSubmit={onSubmit}
      >
        <div className="mb-4 flex items-center justify-between gap-3">
          <h4 className="text-lg font-semibold">
            {mode === "create" ? "New command" : "Edit command"}
          </h4>
          <button
            aria-label="Close command form"
            className="flex h-9 w-9 items-center justify-center rounded-md border border-line bg-white text-ink/70 transition hover:bg-panel hover:text-ink"
            type="button"
            onClick={onClose}
          >
            <XCircle className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>

        <div className="grid gap-3">
          <label className="text-sm font-medium">
            Name
            <input
              className="mt-1 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-ink"
              value={formState.name}
              required
              onChange={(event) => onChange({ ...formState, name: event.target.value })}
            />
          </label>
          <label className="text-sm font-medium">
            Command
            <input
              className="mt-1 w-full rounded-md border border-line bg-white px-3 py-2 font-mono text-sm outline-none transition focus:border-ink"
              value={formState.command}
              required
              onChange={(event) => onChange({ ...formState, command: event.target.value })}
            />
          </label>
          <label className="text-sm font-medium">
            Description
            <textarea
              className="mt-1 min-h-20 w-full resize-y rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-ink"
              value={formState.description}
              onChange={(event) => onChange({ ...formState, description: event.target.value })}
            />
          </label>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="text-sm font-medium">
              Icon
              <input
                className="mt-1 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-ink"
                value={formState.icon}
                onChange={(event) => onChange({ ...formState, icon: event.target.value })}
              />
            </label>
            <div>
              <label className="text-sm font-medium">
                Hotkey
                <input
                  className="mt-1 w-full rounded-md border border-line bg-white px-3 py-2 font-mono outline-none transition focus:border-ink"
                  value={formState.hotkey}
                  maxLength={1}
                  placeholder="d"
                  aria-describedby="hotkey-help"
                  onChange={(event) =>
                    onChange({ ...formState, hotkey: normalizeHotkey(event.target.value) })
                  }
                />
              </label>
              <p id="hotkey-help" className="mt-1 text-xs text-ink/55">
                One letter or digit, pressed with Ctrl/Cmd + Alt.
              </p>
            </div>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="text-sm font-medium">
              Role
              <select
                className="mt-1 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-ink"
                value={formState.role}
                onChange={(event) => {
                  const role = event.target.value as CommandRole;
                  onChange({ ...formState, role, isService: role === "dev" });
                }}
              >
                {ROLE_ORDER.map((role) => (
                  <option key={role} value={role}>
                    {ROLE_LABEL[role]}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-sm font-medium">
              Working directory
              <input
                className="mt-1 w-full rounded-md border border-line bg-white px-3 py-2 font-mono text-sm outline-none transition focus:border-ink"
                placeholder="backend"
                value={formState.cwd}
                onChange={(event) => onChange({ ...formState, cwd: event.target.value })}
              />
              <span className="mt-1 block text-xs font-normal text-ink/55">
                Relative to the app root. Leave empty to use it directly.
              </span>
            </label>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="text-sm font-medium">
              Port
              <input
                className="mt-1 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-ink"
                inputMode="numeric"
                placeholder="8000"
                value={formState.port}
                onChange={(event) => onChange({ ...formState, port: event.target.value })}
              />
            </label>
            <label className="text-sm font-medium">
              Timeout (seconds)
              <input
                className="mt-1 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-ink"
                inputMode="numeric"
                placeholder="900"
                value={formState.timeoutSeconds}
                onChange={(event) =>
                  onChange({ ...formState, timeoutSeconds: event.target.value })
                }
              />
            </label>
          </div>
          <label className="flex items-center gap-3 rounded-md border border-line bg-white p-3 text-sm font-medium">
            <input
              checked={formState.enabled}
              type="checkbox"
              onChange={(event) => onChange({ ...formState, enabled: event.target.checked })}
            />
            Enabled
          </label>
          <label className="flex items-start gap-3 rounded-md border border-line bg-white p-3 text-sm font-medium">
            <input
              checked={formState.isService}
              className="mt-0.5"
              type="checkbox"
              onChange={(event) =>
                onChange({ ...formState, isService: event.target.checked })
              }
            />
            <span>
              Long-running service
              <span className="mt-1 block text-xs font-normal text-ink/55">
                Runs as a service in the Preview tab (Run/Stop/Restart, its own preview URL)
                instead of a one-shot task.
              </span>
            </span>
          </label>
          {formState.isService && (
            <>
              <label className="text-sm font-medium">
                Health check path
                <input
                  className="mt-1 w-full rounded-md border border-line bg-white px-3 py-2 font-mono text-sm outline-none transition focus:border-ink"
                  placeholder="/healthz"
                  value={formState.healthPath}
                  onChange={(event) => onChange({ ...formState, healthPath: event.target.value })}
                />
                <span className="mt-1 block text-xs font-normal text-ink/55">
                  A root-relative HTTP path. Leave empty to fall back to a TCP port check.
                </span>
              </label>
              <label className="flex items-start gap-3 rounded-md border border-line bg-white p-3 text-sm font-medium">
                <input
                  checked={formState.isEntryPoint}
                  className="mt-0.5"
                  type="checkbox"
                  onChange={(event) =>
                    onChange({ ...formState, isEntryPoint: event.target.checked })
                  }
                />
                <span>
                  Entry point
                  <span className="mt-1 block text-xs font-normal text-ink/55">
                    The "Open app" service shown first in the Preview tab. Only one service can be
                    the entry point.
                  </span>
                </span>
              </label>
            </>
          )}
          <label className="flex items-start gap-3 rounded-md border border-line bg-white p-3 text-sm font-medium">
            <input
              checked={formState.requiresConfirmation}
              className="mt-0.5"
              type="checkbox"
              onChange={(event) =>
                onChange({ ...formState, requiresConfirmation: event.target.checked })
              }
            />
            <span>
              Ask me first
              <span className="mt-1 block text-xs font-normal text-ink/55">
                Prompts before running. This is about intent, not permission — every command you
                save is allowed to run.
              </span>
            </span>
          </label>
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <button
            className="rounded-md border border-line bg-white px-4 py-2 text-sm font-semibold transition hover:bg-panel"
            type="button"
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            className="rounded-md bg-primary px-4 py-2 text-sm font-semibold text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45"
            type="submit"
            disabled={isSaving}
          >
            {isSaving ? "Saving..." : "Save"}
          </button>
        </div>
      </form>
    </div>
  );
}

function ConfirmationModal({
  command,
  onCancel,
  onConfirm,
}: {
  command: CustomCommandResponse;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/45 p-4">
      <div className="w-full max-w-md rounded-md border border-line bg-white p-5 shadow-soft">
        <div className="flex items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-line text-warn">
            <AlertTriangle className="h-5 w-5" aria-hidden="true" />
          </div>
          <div className="min-w-0">
            <h4 className="text-lg font-semibold">Confirm command</h4>
            <p className="mt-2 text-sm text-ink/70">{command.name}</p>
            <code className="mt-3 block break-words rounded-md bg-white px-3 py-2 text-xs text-ink/75">
              {command.command}
            </code>
          </div>
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <button
            className="rounded-md border border-line bg-white px-4 py-2 text-sm font-semibold transition hover:bg-panel"
            type="button"
            onClick={onCancel}
          >
            Cancel
          </button>
          <button
            className="rounded-md bg-primary px-4 py-2 text-sm font-semibold text-white transition hover:bg-primary-active"
            type="button"
            onClick={onConfirm}
          >
            Confirm run
          </button>
        </div>
      </div>
    </div>
  );
}

function formPayload(formState: CommandFormState): CustomCommandPayload {
  const port = Number.parseInt(formState.port, 10);
  const timeout = Number.parseInt(formState.timeoutSeconds, 10);
  return {
    name: formState.name,
    command: formState.command,
    description: formState.description || null,
    icon: formState.icon || null,
    hotkey: formState.hotkey || null,
    requires_confirmation: formState.requiresConfirmation,
    role: formState.role,
    kind: formState.isService ? "service" : "task",
    cwd: formState.cwd.trim() || null,
    port: Number.isNaN(port) ? null : port,
    timeout_seconds: Number.isNaN(timeout) ? null : timeout,
    enabled: formState.enabled,
    // The database CHECK rejects is_entry_point on a non-service row, so a task never sends it.
    health_path: formState.isService ? formState.healthPath.trim() || null : null,
    is_entry_point: formState.isService && formState.isEntryPoint,
  };
}
