import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  AlertCircle,
  CheckCircle2,
  CircleDashed,
  CircleDot,
  Clock,
  Loader2,
  Lock,
  Play,
  RotateCcw,
  XCircle,
} from "lucide-react";

import {
  ApiError,
  approveSpecPhase,
  createSpecWorkflow,
  getAgentStreamUrl,
  getSpecArtifact,
  getSpecWorkflow,
  listSpecWorkflows,
  requestSpecChanges,
  runSpecPhase,
  type AgentEventResponse,
  type SpecPhaseName,
  type SpecPhaseResponse,
  type SpecPhaseStatus,
  type SpecWorkflowResponse,
  type WorkspaceResponse,
} from "../../lib/api";
import { ToolCard, pairToolEvents } from "../AgentTab/ToolCard";

const PHASE_ORDER: SpecPhaseName[] = [
  "constitution",
  "specify",
  "clarify",
  "plan",
  "analyze",
  "tasks",
  "implement",
];

const PHASE_LABELS: Record<SpecPhaseName, string> = {
  constitution: "Constitution",
  specify: "Specify",
  clarify: "Clarify",
  plan: "Plan",
  analyze: "Analyze",
  tasks: "Tasks",
  implement: "Implement",
};

// Mirror of the server-side prerequisites (the gate is enforced in the service layer;
// this only drives which Run buttons are enabled in the UI).
const PREREQUISITES: Record<SpecPhaseName, SpecPhaseName[]> = {
  constitution: [],
  specify: [],
  clarify: ["specify"],
  plan: ["specify"],
  analyze: ["plan"],
  tasks: ["plan"],
  implement: ["plan", "tasks"],
};

const ACTIVE_WORKFLOW_STATUSES = new Set(["pending", "running"]);

export function SpecPanel({ workspace }: { workspace: WorkspaceResponse }) {
  const workspaceId = workspace.id;
  const [workflows, setWorkflows] = useState<SpecWorkflowResponse[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const loadWorkflows = useCallback(async () => {
    try {
      const items = await listSpecWorkflows(workspaceId);
      setWorkflows(items);
      setLoadError(null);
      setSelectedId((current) => current ?? items[0]?.id ?? null);
    } catch (error) {
      setLoadError(error instanceof ApiError ? error.message : "Failed to load spec workflows");
    } finally {
      setLoading(false);
    }
  }, [workspaceId]);

  useEffect(() => {
    setLoading(true);
    void loadWorkflows();
  }, [loadWorkflows]);

  const selected = workflows.find((workflow) => workflow.id === selectedId) ?? null;

  return (
    <section className="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
      <div className="rounded-md border border-line bg-white p-4 shadow-soft">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold uppercase tracking-[0.14em] text-muted">
            Spec workflows
          </h3>
        </div>
        <CreateWorkflowForm
          workspaceId={workspaceId}
          onCreated={(workflow) => {
            setWorkflows((current) => [workflow, ...current]);
            setSelectedId(workflow.id);
          }}
        />
        {loading ? (
          <p className="mt-4 text-sm text-muted">Loading…</p>
        ) : loadError ? (
          <p className="mt-4 rounded-md border border-danger/30 bg-[#fff5f2] p-3 text-sm text-danger">
            {loadError}
          </p>
        ) : workflows.length === 0 ? (
          <p className="mt-4 text-sm text-muted">
            No workflows yet. Describe a feature above to start one.
          </p>
        ) : (
          <ul className="mt-4 flex flex-col gap-1">
            {workflows.map((workflow) => (
              <li key={workflow.id}>
                <button
                  type="button"
                  onClick={() => setSelectedId(workflow.id)}
                  className={`w-full rounded-md px-3 py-2 text-left text-sm transition ${
                    workflow.id === selectedId
                      ? "bg-primary text-white"
                      : "text-body hover:bg-panel"
                  }`}
                >
                  <span className="block truncate font-medium">{workflow.feature_name}</span>
                  <span
                    className={`block truncate text-xs ${
                      workflow.id === selectedId ? "text-white/80" : "text-muted"
                    }`}
                  >
                    {workflow.feature_slug} · {workflow.status.replace("_", " ")}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="min-w-0">
        {selected ? (
          <WorkflowDetail
            key={selected.id}
            workspaceId={workspaceId}
            initialWorkflow={selected}
            onWorkflowUpdated={(updated) =>
              setWorkflows((current) =>
                current.map((workflow) => (workflow.id === updated.id ? updated : workflow)),
              )
            }
          />
        ) : (
          <div className="rounded-md border border-dashed border-line bg-white p-8 text-center text-sm text-muted">
            Select or start a spec workflow to view its phases.
          </div>
        )}
      </div>
    </section>
  );
}

function CreateWorkflowForm({
  workspaceId,
  onCreated,
}: {
  workspaceId: string;
  onCreated: (workflow: SpecWorkflowResponse) => void;
}) {
  const [featureName, setFeatureName] = useState("");
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!featureName.trim() || !description.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      const workflow = await createSpecWorkflow(workspaceId, {
        feature_name: featureName.trim(),
        description: description.trim(),
      });
      onCreated(workflow);
      setFeatureName("");
      setDescription("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to start workflow");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-2">
      <input
        className="w-full rounded-md border border-line bg-white px-3 py-2 text-sm outline-none transition focus:border-ink"
        placeholder="Feature name (e.g. Add SSO login)"
        value={featureName}
        onChange={(event) => setFeatureName(event.target.value)}
      />
      <textarea
        className="min-h-[72px] w-full rounded-md border border-line bg-white px-3 py-2 text-sm outline-none transition focus:border-ink"
        placeholder="Describe what you want built…"
        value={description}
        onChange={(event) => setDescription(event.target.value)}
      />
      {error && <p className="text-xs text-danger">{error}</p>}
      <button
        type="submit"
        disabled={submitting || !featureName.trim() || !description.trim()}
        className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-semibold text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:opacity-50"
      >
        {submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
        Start spec workflow
      </button>
    </form>
  );
}

function WorkflowDetail({
  workspaceId,
  initialWorkflow,
  onWorkflowUpdated,
}: {
  workspaceId: string;
  initialWorkflow: SpecWorkflowResponse;
  onWorkflowUpdated: (workflow: SpecWorkflowResponse) => void;
}) {
  const [workflow, setWorkflow] = useState<SpecWorkflowResponse>(initialWorkflow);
  const [activePhase, setActivePhase] = useState<SpecPhaseName>(initialWorkflow.current_phase);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyPhase, setBusyPhase] = useState<SpecPhaseName | null>(null);

  const refresh = useCallback(async () => {
    try {
      const updated = await getSpecWorkflow(workspaceId, workflow.id);
      setWorkflow(updated);
      onWorkflowUpdated(updated);
    } catch {
      // transient; the next poll will retry
    }
  }, [workspaceId, workflow.id, onWorkflowUpdated]);

  // Poll while the workflow has a phase running so the finalizer's transition to
  // awaiting_review/completed is reflected without a manual refresh.
  useEffect(() => {
    if (!ACTIVE_WORKFLOW_STATUSES.has(workflow.status)) return;
    const timer = setInterval(() => void refresh(), 3000);
    return () => clearInterval(timer);
  }, [workflow.status, refresh]);

  const phaseByName = (name: SpecPhaseName): SpecPhaseResponse | undefined =>
    workflow.phases.find((phase) => phase.phase === name);

  const isApproved = (name: SpecPhaseName) => phaseByName(name)?.status === "approved";
  const canRun = (name: SpecPhaseName) =>
    PREREQUISITES[name].every(isApproved) && busyPhase === null;

  async function handleRun(name: SpecPhaseName) {
    setBusyPhase(name);
    setActionError(null);
    try {
      await runSpecPhase(workspaceId, workflow.id, name);
      await refresh();
      setActivePhase(name);
    } catch (error) {
      setActionError(error instanceof ApiError ? error.message : "Failed to run phase");
    } finally {
      setBusyPhase(null);
    }
  }

  async function handleApprove(name: SpecPhaseName) {
    setBusyPhase(name);
    setActionError(null);
    try {
      await approveSpecPhase(workspaceId, workflow.id, name);
      await refresh();
    } catch (error) {
      setActionError(error instanceof ApiError ? error.message : "Failed to approve");
    } finally {
      setBusyPhase(null);
    }
  }

  async function handleRequestChanges(name: SpecPhaseName, notes: string) {
    setBusyPhase(name);
    setActionError(null);
    try {
      await requestSpecChanges(workspaceId, workflow.id, name, notes);
      await refresh();
      setActivePhase(name);
    } catch (error) {
      setActionError(error instanceof ApiError ? error.message : "Failed to request changes");
    } finally {
      setBusyPhase(null);
    }
  }

  const activePhaseRow = phaseByName(activePhase);

  return (
    <div className="flex flex-col gap-4">
      <div className="rounded-md border border-line bg-white p-5 shadow-soft">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="truncate text-lg font-semibold text-ink">{workflow.feature_name}</h2>
            <p className="mt-1 text-sm text-muted">
              Branch <code className="rounded bg-panel px-1.5 py-0.5 text-xs">{workflow.branch}</code>{" "}
              · {workflow.spec_dir}
            </p>
          </div>
          <SpecStatusBadge status={workflow.status} />
        </div>

        <ol className="mt-5 flex flex-wrap gap-2">
          {PHASE_ORDER.map((name) => {
            const phase = phaseByName(name);
            const status = phase?.status ?? "pending";
            return (
              <li key={name}>
                <button
                  type="button"
                  onClick={() => setActivePhase(name)}
                  className={`flex items-center gap-2 rounded-full border px-3 py-1.5 text-sm transition ${
                    activePhase === name
                      ? "border-ink bg-ink text-white"
                      : "border-line bg-white text-body hover:bg-panel"
                  }`}
                >
                  <PhaseStatusIcon status={status} muted={activePhase === name} />
                  {PHASE_LABELS[name]}
                </button>
              </li>
            );
          })}
        </ol>
      </div>

      {actionError && (
        <div className="flex items-center gap-2 rounded-md border border-danger/30 bg-[#fff5f2] px-3 py-2 text-sm text-danger">
          <AlertCircle className="h-4 w-4" />
          {actionError}
        </div>
      )}

      {activePhaseRow && (
        <PhaseDetail
          workspaceId={workspaceId}
          workflow={workflow}
          phase={activePhaseRow}
          busy={busyPhase === activePhase}
          canRun={canRun(activePhase)}
          implementUnlocked={isApproved("plan") && isApproved("tasks")}
          onRun={() => handleRun(activePhase)}
          onApprove={() => handleApprove(activePhase)}
          onRequestChanges={(notes) => handleRequestChanges(activePhase, notes)}
        />
      )}
    </div>
  );
}

function PhaseDetail({
  workspaceId,
  workflow,
  phase,
  busy,
  canRun,
  implementUnlocked,
  onRun,
  onApprove,
  onRequestChanges,
}: {
  workspaceId: string;
  workflow: SpecWorkflowResponse;
  phase: SpecPhaseResponse;
  busy: boolean;
  canRun: boolean;
  implementUnlocked: boolean;
  onRun: () => void;
  onApprove: () => void;
  onRequestChanges: (notes: string) => void;
}) {
  const [artifact, setArtifact] = useState<string | null>(null);
  const [artifactError, setArtifactError] = useState<string | null>(null);
  const [notesOpen, setNotesOpen] = useState(false);
  const [notes, setNotes] = useState("");
  const [confirmImplement, setConfirmImplement] = useState(false);

  const hasArtifact = phase.status === "awaiting_review" || phase.status === "approved" || phase.status === "completed" || phase.status === "changes_requested";

  useEffect(() => {
    setArtifact(null);
    setArtifactError(null);
    if (!hasArtifact) return;
    let cancelled = false;
    getSpecArtifact(workspaceId, workflow.id, phase.phase)
      .then((response) => {
        if (!cancelled) setArtifact(response.content);
      })
      .catch((error) => {
        if (!cancelled)
          setArtifactError(error instanceof ApiError ? error.message : "Failed to load artifact");
      });
    return () => {
      cancelled = true;
    };
  }, [workspaceId, workflow.id, phase.phase, phase.status, phase.attempt, hasArtifact]);

  const isImplement = phase.phase === "implement";
  const runDisabled = busy || !canRun || (isImplement && !implementUnlocked);

  return (
    <div className="rounded-md border border-line bg-white p-5 shadow-soft">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <h3 className="text-base font-semibold text-ink">{PHASE_LABELS[phase.phase]}</h3>
          <SpecStatusBadge status={phase.status} />
          {phase.attempt > 1 && (
            <span className="text-xs text-muted">attempt {phase.attempt}</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {phase.status === "awaiting_review" && (
            <>
              <button
                type="button"
                onClick={onApprove}
                disabled={busy}
                className="inline-flex items-center gap-1.5 rounded-md bg-signal px-3 py-1.5 text-sm font-semibold text-white transition hover:opacity-90 disabled:opacity-50"
              >
                <CheckCircle2 className="h-4 w-4" /> Approve
              </button>
              <button
                type="button"
                onClick={() => setNotesOpen((open) => !open)}
                disabled={busy}
                className="inline-flex items-center gap-1.5 rounded-md border border-line px-3 py-1.5 text-sm font-semibold text-body transition hover:bg-panel disabled:opacity-50"
              >
                <RotateCcw className="h-4 w-4" /> Request changes
              </button>
            </>
          )}
          {phase.status !== "awaiting_review" && phase.status !== "running" && (
            <button
              type="button"
              onClick={() => (isImplement ? setConfirmImplement(true) : onRun())}
              disabled={runDisabled}
              title={
                isImplement && !implementUnlocked
                  ? "Approve the plan and tasks phases first"
                  : undefined
              }
              className="inline-flex items-center gap-1.5 rounded-md bg-ink px-3 py-1.5 text-sm font-semibold text-white transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {isImplement && !implementUnlocked ? (
                <Lock className="h-4 w-4" />
              ) : busy ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Play className="h-4 w-4" />
              )}
              {phase.status === "pending" ? "Run" : "Re-run"}
            </button>
          )}
        </div>
      </div>

      {phase.review_notes && phase.status === "changes_requested" && (
        <p className="mt-3 rounded-md border border-warn/30 bg-[#fffbf0] p-3 text-sm text-warn">
          Requested changes: {phase.review_notes}
        </p>
      )}

      {notesOpen && phase.status === "awaiting_review" && (
        <div className="mt-3 flex flex-col gap-2">
          <textarea
            className="min-h-[80px] w-full rounded-md border border-line bg-white px-3 py-2 text-sm outline-none transition focus:border-ink"
            placeholder="What should change?"
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
          />
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => {
                setNotesOpen(false);
                setNotes("");
              }}
              className="rounded-md border border-line px-3 py-1.5 text-sm text-body transition hover:bg-panel"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={!notes.trim() || busy}
              onClick={() => {
                onRequestChanges(notes.trim());
                setNotesOpen(false);
                setNotes("");
              }}
              className="rounded-md bg-primary px-3 py-1.5 text-sm font-semibold text-white transition hover:bg-primary-active disabled:opacity-50"
            >
              Submit & re-run
            </button>
          </div>
        </div>
      )}

      <div className="mt-4">
        {phase.status === "running" && phase.agent_run_id ? (
          <PhaseStream workspaceId={workspaceId} runId={phase.agent_run_id} />
        ) : phase.status === "error" ? (
          <p className="rounded-md border border-danger/30 bg-[#fff5f2] p-3 text-sm text-danger">
            This phase ended with an error. Re-run it to try again.
          </p>
        ) : artifactError ? (
          <p className="rounded-md border border-danger/30 bg-[#fff5f2] p-3 text-sm text-danger">
            {artifactError}
          </p>
        ) : hasArtifact && artifact !== null ? (
          artifact.trim() ? (
            <ArtifactMarkdown content={artifact} />
          ) : (
            <p className="text-sm text-muted">The artifact file is empty.</p>
          )
        ) : hasArtifact ? (
          <p className="text-sm text-muted">Loading artifact…</p>
        ) : (
          <p className="text-sm text-muted">
            This phase has not produced an artifact yet. Run it to generate one.
          </p>
        )}
      </div>

      {confirmImplement && (
        <ConfirmModal
          title="Approve & run implementation"
          body="The implementation phase will run the agent against the feature worktree and commit code to the feature branch. Proceed?"
          confirmLabel="Run implementation"
          onCancel={() => setConfirmImplement(false)}
          onConfirm={() => {
            setConfirmImplement(false);
            onRun();
          }}
        />
      )}
    </div>
  );
}

function PhaseStream({ workspaceId, runId }: { workspaceId: string; runId: string }) {
  const [events, setEvents] = useState<AgentEventResponse[]>([]);
  const [error, setError] = useState<string | null>(null);
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    setEvents([]);
    setError(null);
    const socket = new WebSocket(getAgentStreamUrl(workspaceId, runId));
    socketRef.current = socket;
    socket.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data) as AgentEventResponse;
        setEvents((prev) => [...prev, message]);
      } catch {
        // ignore malformed frames
      }
    };
    socket.onerror = () => setError("Live stream disconnected.");
    return () => {
      socket.close();
      socketRef.current = null;
    };
  }, [workspaceId, runId]);

  const paired = pairToolEvents(events);
  const outputText = events
    .filter((event) => event.event_type === "output_text")
    .map((event) => event.content)
    .join("");

  return (
    <div className="flex flex-col gap-3" data-testid="spec-phase-stream">
      <div className="flex items-center gap-2 text-sm text-primary">
        <CircleDot className="h-4 w-4 animate-pulse" />
        Agent is working on this phase…
      </div>
      {error && <p className="text-xs text-danger">{error}</p>}
      {outputText && (
        <div className="whitespace-pre-wrap text-sm leading-6 text-body">{outputText}</div>
      )}
      <div className="space-y-2">
        {paired.map((pe) => (
          <ToolCard
            key={pe.toolCallId ?? `${pe.call?.id ?? ""}-${pe.result?.id ?? ""}`}
            event={pe}
          />
        ))}
      </div>
    </div>
  );
}

function ArtifactMarkdown({ content }: { content: string }) {
  return (
    <div className="max-h-[560px] overflow-auto rounded-lg border border-line bg-panel p-4 text-sm leading-6 text-ink">
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: (props) => <h1 className="mb-3 mt-4 text-xl font-semibold first:mt-0" {...props} />,
          h2: (props) => <h2 className="mb-2 mt-4 text-lg font-semibold first:mt-0" {...props} />,
          h3: (props) => <h3 className="mb-2 mt-3 text-base font-semibold" {...props} />,
          p: (props) => <p className="mb-3" {...props} />,
          ul: (props) => <ul className="mb-3 list-disc pl-5" {...props} />,
          ol: (props) => <ol className="mb-3 list-decimal pl-5" {...props} />,
          li: (props) => <li className="mb-1" {...props} />,
          a: (props) => <a className="text-primary underline" {...props} />,
          code: (props) => (
            <code className="rounded bg-white px-1.5 py-0.5 font-mono text-xs" {...props} />
          ),
          pre: (props) => (
            <pre
              className="mb-3 overflow-x-auto rounded-md border border-line bg-white p-3 font-mono text-xs"
              {...props}
            />
          ),
          table: (props) => (
            <table className="mb-3 w-full border-collapse text-left text-xs" {...props} />
          ),
          th: (props) => <th className="border border-line px-2 py-1 font-semibold" {...props} />,
          td: (props) => <td className="border border-line px-2 py-1" {...props} />,
        }}
      >
        {content}
      </Markdown>
    </div>
  );
}

function ConfirmModal({
  title,
  body,
  confirmLabel,
  onCancel,
  onConfirm,
}: {
  title: string;
  body: string;
  confirmLabel: string;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/45 p-4">
      <div className="w-full max-w-md rounded-lg border border-line bg-white p-5 shadow-soft">
        <h3 className="text-base font-semibold text-ink">{title}</h3>
        <p className="mt-2 text-sm text-body">{body}</p>
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md border border-line px-3 py-1.5 text-sm text-body transition hover:bg-panel"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="rounded-md bg-ink px-3 py-1.5 text-sm font-semibold text-white transition hover:opacity-90"
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

function PhaseStatusIcon({ status, muted }: { status: SpecPhaseStatus; muted?: boolean }) {
  const className = "h-3.5 w-3.5";
  switch (status) {
    case "approved":
    case "completed":
      return <CheckCircle2 className={`${className} ${muted ? "text-white" : "text-signal"}`} />;
    case "awaiting_review":
      return <Clock className={`${className} ${muted ? "text-white" : "text-warn"}`} />;
    case "running":
      return <Loader2 className={`${className} animate-spin ${muted ? "text-white" : "text-primary"}`} />;
    case "changes_requested":
      return <RotateCcw className={`${className} ${muted ? "text-white" : "text-warn"}`} />;
    case "error":
      return <XCircle className={`${className} ${muted ? "text-white" : "text-danger"}`} />;
    default:
      return <CircleDashed className={`${className} ${muted ? "text-white" : "text-muted"}`} />;
  }
}

function SpecStatusBadge({ status }: { status: string }) {
  const tone: Record<string, string> = {
    pending: "bg-white text-muted ring-1 ring-line",
    running: "bg-line text-warn",
    awaiting_review: "bg-[#fffbf0] text-warn ring-1 ring-warn/30",
    approved: "bg-white text-signal ring-1 ring-line",
    completed: "bg-white text-signal ring-1 ring-line",
    changes_requested: "bg-[#fffbf0] text-warn ring-1 ring-warn/30",
    error: "bg-[#fff5f2] text-danger",
  };
  const className = tone[status] ?? "bg-white text-muted ring-1 ring-line";
  return (
    <span
      className={`inline-flex shrink-0 items-center rounded-full px-2.5 py-1 text-xs font-semibold capitalize ${className}`}
    >
      {status.replace("_", " ")}
    </span>
  );
}
