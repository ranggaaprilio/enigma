import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { Check, Eye, FlaskConical, Loader2, Play, X } from "lucide-react";

import {
  approveVerificationRun,
  createVerificationRun,
  getVerificationRun,
  listVerificationRuns,
  rejectVerificationRun,
  type VerificationEvidenceResponse,
  type VerificationRunCreatePayload,
  type VerificationRunDetailResponse,
  type VerificationRunResponse,
  type VerificationRunStatus,
  type WorkspaceResponse,
} from "../lib/api";
import { usePermissions } from "../auth/permissions";

/** Human Evidence View: trigger a verification run, inspect the evidence it collected, and
 * approve or reject it at the human gate. */
export function VerificationPanel({ workspace }: { workspace: WorkspaceResponse }) {
  const { can } = usePermissions();
  const canDrive = can("agent.run");

  const [runs, setRuns] = useState<VerificationRunResponse[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<VerificationRunDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [gateBusy, setGateBusy] = useState(false);

  const loadRuns = useCallback(async () => {
    const items = await listVerificationRuns(workspace.id);
    // Merge rather than replace, so a run created while this request was in flight survives.
    setRuns((current) => {
      const fetched = new Set(items.map((r) => r.id));
      return [...(current ?? []).filter((r) => !fetched.has(r.id)), ...items];
    });
    setSelectedId((current) => current ?? items[0]?.id ?? null);
  }, [workspace.id]);

  useEffect(() => {
    if (!canDrive) return;
    loadRuns().catch((err: unknown) => {
      setRuns((current) => current ?? []);
      setError(messageOf(err, "Unable to load verification runs"));
    });
  }, [canDrive, loadRuns]);

  const selectedRef = useRef(selectedId);
  useEffect(() => {
    selectedRef.current = selectedId;
  }, [selectedId]);

  const loadDetail = useCallback(
    async (runId: string) => {
      const value = await getVerificationRun(workspace.id, runId);
      // Ignore a response for a run that is no longer selected.
      if (selectedRef.current === value.id) setDetail(value);
    },
    [workspace.id],
  );

  useEffect(() => {
    if (!selectedId || detail?.id === selectedId) return;
    loadDetail(selectedId).catch((err: unknown) =>
      setError(messageOf(err, "Unable to load verification run")),
    );
  }, [selectedId, detail?.id, loadDetail]);

  function handleCreated(created: VerificationRunDetailResponse) {
    setRuns((current) => [created, ...(current ?? []).filter((r) => r.id !== created.id)]);
    setDetail(created);
    setSelectedId(created.id);
  }

  async function handleCreateFailed(message: string) {
    setError(message);
    // The run may exist server-side (e.g. the request timed out mid-loop); show it if so.
    await loadRuns().catch(() => undefined);
  }

  // Only ever show (and gate) the run that is actually selected.
  const shownDetail = detail && detail.id === selectedId ? detail : null;

  async function handleGate(approve: boolean) {
    if (!shownDetail) return;
    const runId = shownDetail.id;
    setGateBusy(true);
    setError(null);
    try {
      const updated = await (approve ? approveVerificationRun : rejectVerificationRun)(
        workspace.id,
        runId,
      );
      setRuns((current) => (current ?? []).map((r) => (r.id === updated.id ? updated : r)));
      setDetail((current) => (current && current.id === updated.id ? { ...current, ...updated } : current));
    } catch (err) {
      setError(messageOf(err, "Unable to update verification run"));
      // e.g. 409 "not awaiting review": show the run's real status instead of stale buttons.
      await loadDetail(runId).catch(() => undefined);
    } finally {
      setGateBusy(false);
    }
  }

  if (!canDrive) {
    return (
      <section className="rounded-md border border-line bg-white p-5 text-sm text-ink/60">
        You need permission to run agents in this workspace to view verification runs.
      </section>
    );
  }

  return (
    <section className="rounded-md border border-line bg-white p-5">
      <div className="mb-4">
        <p className="text-sm font-medium uppercase tracking-[0.16em] text-ink/55">Verification</p>
        <h3 className="mt-1 text-base font-semibold">{workspace.name}</h3>
      </div>

      {error && (
        <div
          role="alert"
          className="mb-4 rounded-md border border-danger/30 bg-[#fff5f2] p-3 text-sm text-danger"
        >
          {error}
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-[340px_minmax(0,1fr)]">
        <div className="space-y-4">
          <TriggerForm
            workspaceId={workspace.id}
            onCreated={handleCreated}
            onError={(message) => (message ? void handleCreateFailed(message) : setError(null))}
          />
          <RunList runs={runs} selectedId={selectedId} onSelect={setSelectedId} />
        </div>

        <div className="min-w-0 rounded-md border border-line bg-white">
          {!shownDetail ? (
            <div className="p-6 text-sm text-ink/60">
              {selectedId ? "Loading run…" : "Select a run to view its evidence."}
            </div>
          ) : (
            <RunDetail run={shownDetail} gateBusy={gateBusy} onGate={handleGate} />
          )}
        </div>
      </div>
    </section>
  );
}

function TriggerForm({
  workspaceId,
  onCreated,
  onError,
}: {
  workspaceId: string;
  onCreated: (run: VerificationRunDetailResponse) => void;
  onError: (message: string | null) => void;
}) {
  const [planRef, setPlanRef] = useState("");
  const [testCommand, setTestCommand] = useState("");
  const [observePort, setObservePort] = useState("");
  const [cwd, setCwd] = useState("");
  const [timeout, setTimeoutValue] = useState("");
  const [maxIterations, setMaxIterations] = useState("1");
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const payload: VerificationRunCreatePayload = {
      plan_ref: planRef.trim(),
      test_command: testCommand.trim(),
      max_iterations: Number(maxIterations) || 1,
    };
    if (observePort.trim()) payload.observe_port = Number(observePort);
    if (cwd.trim()) payload.cwd = cwd.trim();
    if (timeout.trim()) payload.timeout = Number(timeout);

    setBusy(true);
    onError(null);
    try {
      onCreated(await createVerificationRun(workspaceId, payload));
    } catch (err) {
      onError(messageOf(err, "Unable to start verification run"));
    } finally {
      setBusy(false);
    }
  }

  const inputClass =
    "mt-1 w-full rounded-md border border-line bg-white px-3 py-2 text-sm focus:border-primary focus:outline-none";
  const labelClass = "block text-xs font-semibold text-ink/70";

  return (
    <form onSubmit={handleSubmit} className="space-y-3 rounded-md border border-line bg-panel p-4">
      <label className={labelClass}>
        Plan reference
        <input
          className={inputClass}
          value={planRef}
          onChange={(e) => setPlanRef(e.target.value)}
          placeholder="specs/001-feature/plan.md"
          required
        />
      </label>
      <label className={labelClass}>
        Test command
        <input
          className={`${inputClass} font-mono`}
          value={testCommand}
          onChange={(e) => setTestCommand(e.target.value)}
          placeholder="pytest -q"
          required
        />
      </label>
      <label className={labelClass}>
        Observe port (optional)
        <input
          className={inputClass}
          type="number"
          min={1}
          max={65535}
          value={observePort}
          onChange={(e) => setObservePort(e.target.value)}
        />
      </label>
      <details className="text-xs text-ink/70">
        <summary className="cursor-pointer font-semibold">Advanced</summary>
        <div className="mt-2 space-y-3">
          <label className={labelClass}>
            Working directory
            <input className={inputClass} value={cwd} onChange={(e) => setCwd(e.target.value)} />
          </label>
          <label className={labelClass}>
            Timeout (seconds)
            <input
              className={inputClass}
              type="number"
              min={1}
              max={86400}
              value={timeout}
              onChange={(e) => setTimeoutValue(e.target.value)}
            />
          </label>
          <label className={labelClass}>
            Max iterations
            <input
              className={inputClass}
              type="number"
              min={1}
              max={20}
              value={maxIterations}
              onChange={(e) => setMaxIterations(e.target.value)}
            />
          </label>
        </div>
      </details>
      <button
        type="submit"
        disabled={busy}
        className="flex w-full items-center justify-center gap-2 rounded-lg bg-primary px-3 py-2 text-sm font-semibold text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:opacity-60"
      >
        {busy ? (
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
        ) : (
          <Play className="h-4 w-4" aria-hidden="true" />
        )}
        {busy ? "Running verification…" : "Run verification"}
      </button>
      {busy && (
        <p className="text-xs text-ink/60">
          The run tests, observes and parks at review before returning; this can take a while.
        </p>
      )}
    </form>
  );
}

function RunList({
  runs,
  selectedId,
  onSelect,
}: {
  runs: VerificationRunResponse[] | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  if (runs === null) {
    return <div className="p-4 text-sm text-ink/60">Loading runs…</div>;
  }
  if (runs.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-line bg-white p-4 text-sm text-ink/60">
        No verification runs yet.
      </div>
    );
  }
  return (
    <ul className="space-y-2">
      {runs.map((run) => (
        <li key={run.id}>
          <button
            type="button"
            onClick={() => onSelect(run.id)}
            aria-current={run.id === selectedId ? "true" : undefined}
            className={`w-full rounded-md border p-3 text-left transition ${
              run.id === selectedId ? "border-primary bg-white" : "border-line bg-white hover:bg-panel"
            }`}
          >
            <div className="flex items-start justify-between gap-2">
              <span className="truncate text-sm font-semibold" title={run.plan_ref}>
                {run.plan_ref}
              </span>
              <RunStatusPill status={run.status} />
            </div>
            <p className="mt-1 text-xs text-ink/55">{formatTime(run.created_at)}</p>
          </button>
        </li>
      ))}
    </ul>
  );
}

function RunDetail({
  run,
  gateBusy,
  onGate,
}: {
  run: VerificationRunDetailResponse;
  gateBusy: boolean;
  onGate: (approve: boolean) => void;
}) {
  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line p-4">
        <div className="min-w-0">
          <p className="truncate font-semibold">{run.plan_ref}</p>
          <p className="mt-1 text-xs text-ink/55">Updated {formatTime(run.updated_at)}</p>
        </div>
        <div className="flex items-center gap-2">
          <RunStatusPill status={run.status} />
          {run.status === "review" && (
            <>
              <button
                type="button"
                disabled={gateBusy}
                onClick={() => onGate(true)}
                className="flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-sm font-semibold text-white transition hover:bg-primary-active disabled:opacity-60"
              >
                <Check className="h-4 w-4" aria-hidden="true" />
                Approve
              </button>
              <button
                type="button"
                disabled={gateBusy}
                onClick={() => onGate(false)}
                className="flex items-center gap-1.5 rounded-lg border border-line bg-white px-3 py-1.5 text-sm font-semibold text-ink transition hover:bg-panel disabled:opacity-60"
              >
                <X className="h-4 w-4" aria-hidden="true" />
                Reject
              </button>
            </>
          )}
        </div>
      </div>

      <section aria-label="Evidence" className="space-y-3 p-4">
        {run.evidence.length === 0 ? (
          <p className="text-sm text-ink/60">No evidence recorded for this run.</p>
        ) : (
          run.evidence.map((item) => <EvidenceCard key={item.id} evidence={item} />)
        )}
      </section>
    </div>
  );
}

function EvidenceCard({ evidence }: { evidence: VerificationEvidenceResponse }) {
  const d = evidence.detail;
  const Icon = evidence.kind === "test" ? FlaskConical : Eye;
  const url = stringField(d, "url");
  const errorText = stringField(d, "error");

  return (
    <article className="rounded-md border border-line p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-semibold capitalize">
          <Icon className="h-4 w-4 text-ink/55" aria-hidden="true" />
          {evidence.kind}
        </div>
        <EvidenceStatusPill status={evidence.status} />
      </div>
      <div className="mt-2 space-y-1 text-xs text-ink/75">
        {stringField(d, "command") && (
          <code
            className="block truncate rounded-md bg-panel px-2 py-1"
            title={stringField(d, "command") ?? undefined}
          >
            {stringField(d, "command")}
          </code>
        )}
        {typeof d.exit_code === "number" && <p>Exit code {d.exit_code}</p>}
        {typeof d.timeout === "number" && <p>Timeout {d.timeout}s</p>}
        {stringField(d, "description") && <p>{stringField(d, "description")}</p>}
        {url &&
          (isHttpUrl(url) ? (
            <a href={url} target="_blank" rel="noreferrer" className="block truncate text-primary underline">
              {url}
            </a>
          ) : (
            <p className="truncate">{url}</p>
          ))}
        {evidence.path && (
          <p className="truncate font-mono" title={evidence.path}>
            {evidence.path}
          </p>
        )}
        {errorText && <p className="text-danger">{errorText}</p>}
      </div>
    </article>
  );
}

const RUN_TONE: Record<VerificationRunStatus, string> = {
  planning: "bg-line text-warn",
  testing: "bg-line text-warn",
  observing: "bg-line text-warn",
  review: "bg-white text-primary ring-1 ring-primary/40",
  approved: "bg-line text-signal",
  rejected: "bg-[#fff5f2] text-danger",
  error: "bg-[#fff5f2] text-danger",
};

function RunStatusPill({ status }: { status: VerificationRunStatus }) {
  return (
    <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-semibold ${RUN_TONE[status]}`}>
      {status}
    </span>
  );
}

function EvidenceStatusPill({ status }: { status: VerificationEvidenceResponse["status"] }) {
  const tone = status === "passed" ? "bg-line text-signal" : "bg-[#fff5f2] text-danger";
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${tone}`}>
      {status.replace("_", " ")}
    </span>
  );
}

function stringField(detail: Record<string, unknown>, key: string): string | null {
  const value = detail[key];
  return typeof value === "string" && value ? value : null;
}

/** Only render http(s) URLs as links, so a stored value can never become a `javascript:` href. */
function isHttpUrl(value: string): boolean {
  try {
    const { protocol } = new URL(value);
    return protocol === "http:" || protocol === "https:";
  } catch {
    return false;
  }
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function messageOf(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}
