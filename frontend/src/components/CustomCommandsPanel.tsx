import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Pencil,
  Play,
  Plus,
  TerminalSquare,
  Trash2,
  XCircle,
} from "lucide-react";

import {
  ApiError,
  createCustomCommand,
  deleteCustomCommand,
  getCommandRunStreamUrl,
  listCustomCommandRuns,
  listCustomCommands,
  runCustomCommand,
  updateCustomCommand,
  type CommandRunResponse,
  type CommandRunStatus,
  type CommandRunStreamMessage,
  type CustomCommandPayload,
  type CustomCommandResponse,
  type WorkspaceResponse,
} from "../lib/api";

type ModalMode = "create" | "edit";
type CommandFormState = {
  name: string;
  command: string;
  description: string;
  icon: string;
  hotkey: string;
  requiresConfirmation: boolean;
};

const emptyForm: CommandFormState = {
  name: "",
  command: "",
  description: "",
  icon: "",
  hotkey: "",
  requiresConfirmation: false,
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
  const socketRef = useRef<WebSocket | null>(null);

  const selectedCommand = useMemo(
    () => commands.find((command) => command.id === selectedCommandId) ?? null,
    [commands, selectedCommandId],
  );
  const visibleOutput = activeRun?.status === "running" ? streamOutput : activeRun?.output ?? "";

  useEffect(() => {
    socketRef.current?.close();
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
      socketRef.current?.close();
    };
  }, [workspace.id]);

  useEffect(() => {
    setRuns([]);
    setActiveRun(null);
    setStreamOutput("");
    if (!selectedCommand) {
      return;
    }

    const controller = new AbortController();
    listCustomCommandRuns(workspace.id, selectedCommand.id, controller.signal)
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
  }, [workspace.id, selectedCommand]);

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

  function requestRun(command: CustomCommandResponse) {
    if (command.requires_confirmation) {
      setPendingConfirmation(command);
      return;
    }
    void startRun(command, false);
  }

  async function startRun(command: CustomCommandResponse, confirmed: boolean) {
    setIsRunning(true);
    setError(null);
    setPendingConfirmation(null);
    setStreamOutput("");
    socketRef.current?.close();
    try {
      const run = await runCustomCommand(workspace.id, command.id, { confirmed });
      setActiveRun(run);
      setRuns((current) => [run, ...current.filter((item) => item.id !== run.id)]);
      connectRunStream(run);
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : "Unable to run command.");
    } finally {
      setIsRunning(false);
    }
  }

  function connectRunStream(run: CommandRunResponse) {
    const socket = new window.WebSocket(getCommandRunStreamUrl(workspace.id, run.id));
    socketRef.current = socket;
    socket.onmessage = (message) => {
      const payload = JSON.parse(message.data) as CommandRunStreamMessage;
      if (payload.type === "output") {
        setStreamOutput((current) => `${current}${payload.content}`);
        setActiveRun((current) =>
          current ? { ...current, output: `${current.output}${payload.content}` } : current,
        );
        return;
      }
      setActiveRun((current) =>
        current
          ? {
              ...current,
              status: payload.status,
              exit_code: payload.exit_code,
              completed_at: payload.status === "running" ? null : new Date().toISOString(),
            }
          : current,
      );
      setRuns((current) =>
        current.map((item) =>
          item.id === run.id
            ? { ...item, status: payload.status, exit_code: payload.exit_code }
            : item,
        ),
      );
    };
    socket.onerror = () => setError("Command stream disconnected.");
  }

  return (
    <section className="rounded-md border border-line bg-[#fffdf8] p-5 shadow-soft">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-sm font-medium uppercase tracking-[0.16em] text-ink/55">
            Commands
          </p>
          <h3 className="mt-1 text-base font-semibold">{workspace.name}</h3>
        </div>
        <button
          className="flex items-center gap-2 rounded-md bg-ink px-3 py-2 text-sm font-semibold text-white transition hover:bg-ink/90"
          type="button"
          onClick={openCreateModal}
        >
          <Plus className="h-4 w-4" aria-hidden="true" />
          New command
        </button>
      </div>

      {error && (
        <div className="mb-4 rounded-md border border-danger/30 bg-[#fff5f2] p-3 text-sm text-danger">
          {error}
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-[340px_minmax(0,1fr)]">
        <div className="space-y-3">
          {commands.length === 0 && (
            <div className="rounded-md border border-dashed border-line bg-white p-4 text-sm text-ink/60">
              No commands yet.
            </div>
          )}
          {commands.map((command) => (
            <button
              key={command.id}
              className={`w-full rounded-md border p-4 text-left transition ${
                selectedCommandId === command.id
                  ? "border-ink bg-white shadow-soft"
                  : "border-line bg-white hover:border-ink/45"
              }`}
              type="button"
              onClick={() => setSelectedCommandId(command.id)}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <TerminalSquare className="h-4 w-4 shrink-0 text-ink/55" aria-hidden="true" />
                    <span className="truncate font-semibold">{command.name}</span>
                  </div>
                  <code className="mt-2 block truncate rounded-md bg-[#f5f1e8] px-2 py-1 text-xs text-ink/75">
                    {command.command}
                  </code>
                </div>
                {command.requires_confirmation && (
                  <AlertTriangle className="h-4 w-4 shrink-0 text-warn" aria-label="Requires confirmation" />
                )}
              </div>
              {(command.description || command.hotkey) && (
                <div className="mt-3 flex items-center justify-between gap-2 text-xs text-ink/60">
                  <span className="truncate">{command.description}</span>
                  {command.hotkey && (
                    <kbd className="rounded border border-line bg-[#fffdf8] px-2 py-1 font-mono">
                      {command.hotkey}
                    </kbd>
                  )}
                </div>
              )}
            </button>
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
                  <code className="mt-2 block break-words rounded-md bg-[#f5f1e8] px-2 py-1 text-xs text-ink/75">
                    {selectedCommand.command}
                  </code>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    aria-label="Run command"
                    className="flex h-10 w-10 items-center justify-center rounded-md bg-ink text-white transition hover:bg-ink/90 disabled:cursor-not-allowed disabled:bg-ink/45"
                    type="button"
                    disabled={isRunning || activeRun?.status === "running"}
                    title="Run command"
                    onClick={() => requestRun(selectedCommand)}
                  >
                    <Play className="h-4 w-4" aria-hidden="true" />
                  </button>
                  <button
                    aria-label="Edit command"
                    className="flex h-10 w-10 items-center justify-center rounded-md border border-line bg-white text-ink/70 transition hover:bg-[#f5f1e8] hover:text-ink"
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
                </div>
              </div>

              <div className="grid gap-4 p-4 lg:grid-cols-[minmax(0,1fr)_260px]">
                <div className="min-w-0">
                  <div className="mb-3 flex items-center justify-between gap-3">
                    <h5 className="font-semibold">Output</h5>
                    <CommandStatusPill status={activeRun?.status ?? "idle"} />
                  </div>
                  <pre className="min-h-[360px] max-h-[520px] overflow-auto whitespace-pre-wrap break-words rounded-md border border-[#0b1118] bg-[#111820] p-4 font-mono text-xs leading-5 text-[#f8f4ea]">
                    {visibleOutput || "Run output will appear here."}
                  </pre>
                </div>

                <div className="space-y-2">
                  <h5 className="font-semibold">Run history</h5>
                  {runs.length === 0 && (
                    <div className="rounded-md border border-dashed border-line bg-[#fffdf8] p-3 text-sm text-ink/60">
                      No runs yet.
                    </div>
                  )}
                  {runs.map((run) => (
                    <button
                      key={run.id}
                      className={`w-full rounded-md border p-3 text-left text-sm transition ${
                        activeRun?.id === run.id
                          ? "border-ink bg-[#fffdf8]"
                          : "border-line hover:border-ink/45"
                      }`}
                      type="button"
                      onClick={() => {
                        setActiveRun(run);
                        setStreamOutput(run.output);
                      }}
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
        className="w-full max-w-xl rounded-md border border-line bg-[#fffdf8] p-5 shadow-soft"
        onSubmit={onSubmit}
      >
        <div className="mb-4 flex items-center justify-between gap-3">
          <h4 className="text-lg font-semibold">
            {mode === "create" ? "New command" : "Edit command"}
          </h4>
          <button
            aria-label="Close command form"
            className="flex h-9 w-9 items-center justify-center rounded-md border border-line bg-white text-ink/70 transition hover:bg-[#f5f1e8] hover:text-ink"
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
            <label className="text-sm font-medium">
              Hotkey
              <input
                className="mt-1 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-ink"
                value={formState.hotkey}
                onChange={(event) => onChange({ ...formState, hotkey: event.target.value })}
              />
            </label>
          </div>
          <label className="flex items-center gap-3 rounded-md border border-line bg-white p-3 text-sm font-medium">
            <input
              checked={formState.requiresConfirmation}
              type="checkbox"
              onChange={(event) =>
                onChange({ ...formState, requiresConfirmation: event.target.checked })
              }
            />
            Requires confirmation
          </label>
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <button
            className="rounded-md border border-line bg-white px-4 py-2 text-sm font-semibold transition hover:bg-[#f5f1e8]"
            type="button"
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            className="rounded-md bg-ink px-4 py-2 text-sm font-semibold text-white transition hover:bg-ink/90 disabled:cursor-not-allowed disabled:bg-ink/45"
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
      <div className="w-full max-w-md rounded-md border border-line bg-[#fffdf8] p-5 shadow-soft">
        <div className="flex items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-[#f7eadc] text-warn">
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
            className="rounded-md border border-line bg-white px-4 py-2 text-sm font-semibold transition hover:bg-[#f5f1e8]"
            type="button"
            onClick={onCancel}
          >
            Cancel
          </button>
          <button
            className="rounded-md bg-ink px-4 py-2 text-sm font-semibold text-white transition hover:bg-ink/90"
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

function CommandStatusPill({ status }: { status: CommandRunStatus | "idle" }) {
  const toneClass = {
    idle: "bg-white text-ink/60",
    running: "bg-[#f7eadc] text-warn",
    completed: "bg-[#e3efe9] text-signal",
    failed: "bg-[#fde7e3] text-danger",
    error: "bg-[#fde7e3] text-danger",
  }[status];
  const Icon = status === "completed" ? CheckCircle2 : status === "idle" ? TerminalSquare : XCircle;

  return (
    <span className={`inline-flex items-center gap-2 rounded-full px-3 py-1 text-sm font-semibold capitalize ${toneClass}`}>
      <Icon className="h-4 w-4" aria-hidden="true" />
      {status}
    </span>
  );
}

function formPayload(formState: CommandFormState): CustomCommandPayload {
  return {
    name: formState.name,
    command: formState.command,
    description: formState.description || null,
    icon: formState.icon || null,
    hotkey: formState.hotkey || null,
    requires_confirmation: formState.requiresConfirmation,
  };
}
