import { useEffect, useMemo, useState } from "react";

import { usePermissions } from "../../auth/permissions";
import {
  ApiError,
  bulkDeleteWorkspaceEnvVars,
  bulkUpsertWorkspaceEnvVars,
  getWorkspaceEnvVarTemplate,
  listCustomCommands,
  listWorkspaceEnvVars,
  listWorkspaceServices,
  updateWorkspace,
  upsertWorkspaceEnvVar,
  type AffectedService,
  type CustomCommandResponse,
  type EnvVarBulkResponse,
  type WorkspaceEnvVarResponse,
  type WorkspaceResponse,
  type WorkspaceServiceResponse,
} from "../../lib/api";
import { ConfirmModal } from "../ui/ConfirmModal";
import { TextArea } from "../ui/TextArea";
import { TextInput } from "../ui/TextInput";
import { AffectedServicesBanner } from "./AffectedServicesBanner";

const WORKSPACE_SCOPE_VALUE = "workspace";

type EnvVarGroup = {
  key: string;
  label: string;
  customCommandId: string | null;
  items: WorkspaceEnvVarResponse[];
};

export function EnvVarEditor({
  workspace,
  envVars,
  onEnvVarsChanged,
  onWorkspaceChanged,
}: {
  workspace: WorkspaceResponse;
  envVars: WorkspaceEnvVarResponse[];
  onEnvVarsChanged: (envVars: WorkspaceEnvVarResponse[]) => void;
  onWorkspaceChanged: (workspace: WorkspaceResponse) => void;
}) {
  const { can } = usePermissions();
  const canManage = can("workspace.manageEnv");

  const [customCommands, setCustomCommands] = useState<CustomCommandResponse[]>([]);
  const [services, setServices] = useState<WorkspaceServiceResponse[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [affectedServices, setAffectedServices] = useState<AffectedService[]>([]);
  const [warnings, setWarnings] = useState<string[]>([]);

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [pendingDelete, setPendingDelete] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingValue, setEditingValue] = useState("");
  const [editingBusy, setEditingBusy] = useState(false);

  const [bulkMode, setBulkMode] = useState(false);
  const [bulkScope, setBulkScope] = useState<string>(WORKSPACE_SCOPE_VALUE);
  const [bulkText, setBulkText] = useState("");
  const [bulkPreview, setBulkPreview] = useState<EnvVarBulkResponse | null>(null);
  const [previewedText, setPreviewedText] = useState<string | null>(null);
  const [bulkBusy, setBulkBusy] = useState<"preview" | "apply" | null>(null);

  const [exportLabel, setExportLabel] = useState("Export template");

  const [envFileEnabled, setEnvFileEnabled] = useState(workspace.env_file_path !== null);
  const [envFilePath, setEnvFilePath] = useState(workspace.env_file_path ?? "");
  const [envFileBusy, setEnvFileBusy] = useState(false);
  const [envFileMessage, setEnvFileMessage] = useState<string | null>(null);

  useEffect(() => {
    setEnvFileEnabled(workspace.env_file_path !== null);
    setEnvFilePath(workspace.env_file_path ?? "");
  }, [workspace.env_file_path]);

  useEffect(() => {
    const controller = new AbortController();
    listCustomCommands(workspace.id, controller.signal)
      .then(setCustomCommands)
      .catch(() => undefined);
    listWorkspaceServices(workspace.id, controller.signal)
      .then((response) => setServices(response.services))
      .catch(() => undefined);
    return () => controller.abort();
  }, [workspace.id]);

  const serviceCommands = useMemo(
    () => customCommands.filter((command) => command.kind === "service"),
    [customCommands],
  );

  const commandsById = useMemo(
    () => new Map(customCommands.map((command) => [command.id, command])),
    [customCommands],
  );

  const groups = useMemo<EnvVarGroup[]>(() => {
    const workspaceItems = envVars.filter((envVar) => envVar.scope === "workspace");
    const serviceGroups = new Map<string, WorkspaceEnvVarResponse[]>();
    for (const envVar of envVars) {
      if (envVar.scope !== "service") continue;
      const groupKey = envVar.custom_command_id ?? "unknown";
      const list = serviceGroups.get(groupKey) ?? [];
      list.push(envVar);
      serviceGroups.set(groupKey, list);
    }

    const result: EnvVarGroup[] = [
      {
        key: WORKSPACE_SCOPE_VALUE,
        label: "Workspace-wide",
        customCommandId: null,
        items: workspaceItems,
      },
    ];
    for (const [groupKey, items] of serviceGroups) {
      result.push({
        key: groupKey,
        label: commandsById.get(groupKey)?.name ?? "Unknown service",
        customCommandId: groupKey,
        items,
      });
    }
    return result;
  }, [envVars, commandsById]);

  const noVariablesForServices = services.length > 0 && envVars.length === 0;
  const selectedItems = envVars.filter((envVar) => selected.has(envVar.id));

  function toggleSelected(id: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  function dismissAffected(serviceId: string) {
    setAffectedServices((current) => current.filter((service) => service.service_id !== serviceId));
  }

  async function refreshEnvVars() {
    const fresh = await listWorkspaceEnvVars(workspace.id);
    onEnvVarsChanged(fresh);
  }

  function startEdit(item: WorkspaceEnvVarResponse) {
    setEditingId(item.id);
    setEditingValue("");
    setError(null);
  }

  function cancelEdit() {
    setEditingId(null);
    setEditingValue("");
  }

  async function saveEdit(item: WorkspaceEnvVarResponse) {
    setEditingBusy(true);
    setError(null);
    try {
      const result = await upsertWorkspaceEnvVar(workspace.id, {
        key: item.key,
        value: editingValue,
        custom_command_id: item.custom_command_id,
      });
      await refreshEnvVars();
      setAffectedServices(result.affected_services);
      setWarnings(result.warnings);
      setEditingId(null);
      setEditingValue("");
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : "Unable to save variable.");
    } finally {
      setEditingBusy(false);
    }
  }

  async function confirmDelete() {
    setDeleteBusy(true);
    setError(null);
    try {
      const byScope = new Map<string | null, string[]>();
      for (const item of selectedItems) {
        const list = byScope.get(item.custom_command_id) ?? [];
        list.push(item.key);
        byScope.set(item.custom_command_id, list);
      }

      const affected: AffectedService[] = [];
      for (const [customCommandId, keys] of byScope) {
        const response = await bulkDeleteWorkspaceEnvVars(workspace.id, {
          keys,
          custom_command_id: customCommandId,
        });
        affected.push(...response.affected_services);
      }

      await refreshEnvVars();
      setAffectedServices(affected);
      setSelected(new Set());
      setPendingDelete(false);
    } catch (caughtError) {
      setError(
        caughtError instanceof ApiError ? caughtError.message : "Unable to delete variables.",
      );
    } finally {
      setDeleteBusy(false);
    }
  }

  function bulkCustomCommandId(): string | null {
    return bulkScope === WORKSPACE_SCOPE_VALUE ? null : bulkScope;
  }

  async function runBulkPreview() {
    setBulkBusy("preview");
    setError(null);
    try {
      const result = await bulkUpsertWorkspaceEnvVars(workspace.id, {
        text: bulkText,
        custom_command_id: bulkCustomCommandId(),
        dry_run: true,
      });
      setBulkPreview(result);
      setPreviewedText(bulkText);
      setWarnings(result.warnings);
    } catch (caughtError) {
      setError(
        caughtError instanceof ApiError ? caughtError.message : "Unable to preview changes.",
      );
    } finally {
      setBulkBusy(null);
    }
  }

  async function applyBulk() {
    setBulkBusy("apply");
    setError(null);
    try {
      const result = await bulkUpsertWorkspaceEnvVars(workspace.id, {
        text: bulkText,
        custom_command_id: bulkCustomCommandId(),
        dry_run: false,
      });
      await refreshEnvVars();
      setAffectedServices(result.affected_services);
      setWarnings(result.warnings);
      setBulkPreview(null);
      setPreviewedText(null);
      setBulkText("");
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : "Unable to apply changes.");
    } finally {
      setBulkBusy(null);
    }
  }

  async function exportTemplate() {
    setError(null);
    try {
      const text = await getWorkspaceEnvVarTemplate(workspace.id);
      await navigator.clipboard?.writeText(text);
      setExportLabel("Copied");
      window.setTimeout(() => setExportLabel("Export template"), 1200);
    } catch (caughtError) {
      setError(
        caughtError instanceof ApiError ? caughtError.message : "Unable to export template.",
      );
    }
  }

  async function saveEnvFile() {
    setEnvFileBusy(true);
    setEnvFileMessage(null);
    setError(null);
    try {
      const updated = await updateWorkspace(workspace.id, {
        env_file_path: envFileEnabled ? envFilePath.trim() || null : null,
      });
      onWorkspaceChanged(updated);
      setEnvFileMessage("Saved.");
    } catch (caughtError) {
      setError(
        caughtError instanceof ApiError ? caughtError.message : "Unable to save .env file path.",
      );
    } finally {
      setEnvFileBusy(false);
    }
  }

  const applyDisabled =
    bulkBusy !== null ||
    !bulkText.trim() ||
    previewedText === null ||
    previewedText !== bulkText;

  return (
    <section className="mt-4 rounded-md border border-line bg-white p-5 shadow-soft">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium uppercase tracking-[0.16em] text-ink/55">Workspace</p>
          <h3 className="mt-1 text-base font-semibold">Environment variables</h3>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => void exportTemplate()}
            className="rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel"
          >
            {exportLabel}
          </button>
          {canManage && (
            <button
              type="button"
              onClick={() => setBulkMode((value) => !value)}
              className="rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel"
            >
              {bulkMode ? "Close bulk edit" : "Bulk edit"}
            </button>
          )}
          {canManage && selected.size > 0 && (
            <button
              type="button"
              onClick={() => setPendingDelete(true)}
              className="rounded-md border border-danger/40 bg-white px-3 py-2 text-sm font-medium text-danger transition hover:bg-danger/10"
            >
              Delete selected ({selected.size})
            </button>
          )}
        </div>
      </div>

      <p className="mt-3 rounded-md border border-line bg-panel p-3 text-xs text-ink/65">
        Values cannot be read back once saved — the masked display is a deliberate design
        choice, not a bug. Use "Replace value" to overwrite a key.
      </p>

      {noVariablesForServices && (
        <p className="mt-3 rounded-md border border-warn/30 bg-[#fffbf0] p-3 text-sm text-warn">
          This workspace has service commands but no environment variables yet.
        </p>
      )}

      {warnings.length > 0 && (
        <div className="mt-3 rounded-md border border-warn/30 bg-[#fffbf0] p-3 text-sm text-warn">
          <ul className="list-disc space-y-1 pl-4">
            {warnings.map((warning, index) => (
              <li key={index}>{warning}</li>
            ))}
          </ul>
        </div>
      )}

      {error && (
        <div className="mt-3 rounded-md border border-danger/30 bg-[#fff5f2] p-3 text-sm text-danger">
          {error}
        </div>
      )}

      <AffectedServicesBanner
        workspaceId={workspace.id}
        services={affectedServices}
        onDismiss={dismissAffected}
      />

      {bulkMode && canManage && (
        <div className="mt-4 rounded-md border border-line bg-panel p-4">
          <h4 className="text-sm font-semibold">Bulk edit</h4>
          <div className="mt-3 grid gap-3">
            <label className="block text-sm font-medium">
              Scope
              <select
                className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 text-sm outline-none transition focus:border-ink"
                value={bulkScope}
                onChange={(event) => {
                  setBulkScope(event.target.value);
                  setBulkPreview(null);
                  setPreviewedText(null);
                }}
              >
                <option value={WORKSPACE_SCOPE_VALUE}>Workspace-wide</option>
                {serviceCommands.map((command) => (
                  <option key={command.id} value={command.id}>
                    {command.name}
                  </option>
                ))}
              </select>
            </label>
            <TextArea
              label="Paste .env text"
              value={bulkText}
              onChange={(value) => {
                setBulkText(value);
                setBulkPreview(null);
                setPreviewedText(null);
              }}
              placeholder={"KEY=value\nOTHER_KEY=value"}
            />
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                disabled={bulkBusy !== null || !bulkText.trim()}
                onClick={() => void runBulkPreview()}
                className="rounded-md border border-line bg-white px-4 py-2 text-sm font-semibold transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
              >
                {bulkBusy === "preview" ? "Previewing..." : "Preview"}
              </button>
              <button
                type="button"
                disabled={applyDisabled}
                onClick={() => void applyBulk()}
                className="rounded-md bg-primary px-4 py-2 text-sm font-semibold text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45"
              >
                {bulkBusy === "apply" ? "Applying..." : "Apply"}
              </button>
            </div>

            {bulkPreview && (
              <div className="rounded-md border border-line bg-white p-3 text-sm">
                <p className="font-medium text-ink">
                  {bulkPreview.created} new · {bulkPreview.updated} updated ·{" "}
                  {bulkPreview.unchanged} unchanged · {bulkPreview.invalid} invalid
                </p>
                {bulkPreview.items.some((item) => item.status === "invalid") && (
                  <ul className="mt-2 space-y-1 text-xs text-danger">
                    {bulkPreview.items
                      .filter((item) => item.status === "invalid")
                      .map((item, index) => (
                        <li key={`${item.key}-${index}`}>
                          {item.key || `line ${item.line ?? "?"}`}: {item.message ?? "Invalid entry."}
                        </li>
                      ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        </div>
      )}

      <div className="mt-4 space-y-5">
        {groups.map(
          (group) =>
            (group.items.length > 0 || group.customCommandId === null) && (
              <div key={group.key}>
                <h4 className="text-sm font-semibold text-ink/75">{group.label}</h4>
                <div className="mt-2 space-y-2">
                  {group.items.length === 0 && (
                    <div className="rounded-md border border-dashed border-line bg-white p-4 text-sm text-ink/65">
                      No environment variables yet.
                    </div>
                  )}
                  {group.items.map((item) => (
                    <div
                      key={item.id}
                      className="rounded-md border border-line bg-white p-3"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <div className="flex min-w-0 items-center gap-3">
                          {canManage && (
                            <input
                              type="checkbox"
                              aria-label={`Select ${item.key}`}
                              checked={selected.has(item.id)}
                              onChange={() => toggleSelected(item.id)}
                            />
                          )}
                          <div className="min-w-0">
                            <p className="truncate font-mono text-sm font-semibold">{item.key}</p>
                            <p className="mt-1 font-mono text-sm text-ink/55">
                              {item.masked_value}
                            </p>
                          </div>
                          <span className="shrink-0 rounded-full bg-line px-2 py-0.5 text-xs font-semibold text-ink/60">
                            {group.label}
                          </span>
                        </div>
                        {canManage && (
                          <button
                            type="button"
                            onClick={() =>
                              editingId === item.id ? cancelEdit() : startEdit(item)
                            }
                            className="shrink-0 rounded-md border border-line bg-white px-3 py-1 text-sm font-medium transition hover:bg-panel"
                          >
                            {editingId === item.id ? "Cancel" : "Replace value"}
                          </button>
                        )}
                      </div>

                      {editingId === item.id && (
                        <div className="mt-3 flex flex-wrap items-center gap-2">
                          <div className="min-w-[200px] flex-1">
                            <TextInput
                              label="New value"
                              type="password"
                              value={editingValue}
                              onChange={setEditingValue}
                              autoFocus
                            />
                          </div>
                          <button
                            type="button"
                            disabled={editingBusy}
                            onClick={() => void saveEdit(item)}
                            className="rounded-md bg-primary px-4 py-2 text-sm font-semibold text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45"
                          >
                            {editingBusy ? "Saving..." : "Save"}
                          </button>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            ),
        )}
      </div>

      {canManage && (
        <div className="mt-5 rounded-md border border-line bg-panel p-4">
          <h4 className="text-sm font-semibold">.env file</h4>
          <label className="mt-3 flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={envFileEnabled}
              onChange={(event) => setEnvFileEnabled(event.target.checked)}
            />
            Write a `.env` file inside the sandbox
          </label>
          {envFileEnabled && (
            <div className="mt-3">
              <TextInput
                label="Path"
                value={envFilePath}
                onChange={setEnvFilePath}
                placeholder="apps/api/.env"
                mono
              />
            </div>
          )}
          <p className="mt-2 text-xs text-ink/60">
            Written with mode 600 inside the sandbox and excluded from git; rewritten every time
            a service starts.
          </p>
          <div className="mt-3 flex items-center gap-2">
            <button
              type="button"
              disabled={envFileBusy}
              onClick={() => void saveEnvFile()}
              className="rounded-md border border-line bg-white px-4 py-2 text-sm font-semibold transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
            >
              {envFileBusy ? "Saving..." : "Save"}
            </button>
            {envFileMessage && <span className="text-sm text-signal">{envFileMessage}</span>}
          </div>
        </div>
      )}

      {pendingDelete && (
        <ConfirmModal
          title="Delete selected variables?"
          body={`These variables will be permanently deleted: ${selectedItems
            .map((item) => item.key)
            .join(", ")}`}
          confirmLabel="Delete"
          busy={deleteBusy}
          onCancel={() => setPendingDelete(false)}
          onConfirm={() => void confirmDelete()}
        />
      )}
    </section>
  );
}
