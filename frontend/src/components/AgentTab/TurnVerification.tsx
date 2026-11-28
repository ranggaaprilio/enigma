import { type FormEvent, useState } from "react";
import { Link, useInRouterContext } from "react-router";
import { Loader2, Play, ShieldCheck, X } from "lucide-react";

import {
  verifyAgentRun,
  type VerificationRunResponse,
  type VerificationRunStatus,
  type VerifyAgentRunPayload,
} from "../../lib/api";
import { ACTIVE_STATUSES } from "./useRunVerifications";

const CHIP: Record<VerificationRunStatus, { label: string; className: string }> = {
  planning: { label: "Verifying", className: "text-warn" },
  testing: { label: "Testing", className: "text-warn" },
  fixing: { label: "Fixing", className: "text-warn" },
  observing: { label: "Observing", className: "text-warn" },
  review: { label: "Awaiting review", className: "text-primary" },
  approved: { label: "Approved", className: "text-signal" },
  rejected: { label: "Rejected", className: "text-danger" },
  error: { label: "Error", className: "text-danger" },
};

function VerificationChip({ verification }: { verification: VerificationRunResponse }) {
  const inRouter = useInRouterContext();
  const { label, className } = CHIP[verification.status];
  const classes = `inline-flex items-center gap-1 rounded-full border border-line bg-white px-2 py-0.5 text-xs font-medium ${className}`;
  const content = (
    <>
      {ACTIVE_STATUSES.has(verification.status) ? (
        <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
      ) : (
        <ShieldCheck className="h-3 w-3" aria-hidden="true" />
      )}
      {label}
    </>
  );
  if (!inRouter) {
    return (
      <span className={classes} aria-label={`Verification: ${label}`}>
        {content}
      </span>
    );
  }
  return (
    <Link
      to={`../verify?run=${encodeURIComponent(verification.id)}`}
      relative="path"
      className={`${classes} transition hover:bg-panel`}
      aria-label={`Verification: ${label}`}
      title="Open the verification evidence"
    >
      {content}
    </Link>
  );
}

/** A turn's verification chip, and for a completed run a "Verify" action with a small form. */
export function TurnVerification({
  workspaceId,
  runId,
  canVerify,
  verification,
  onStarted,
}: {
  workspaceId: string;
  runId: string;
  canVerify: boolean;
  verification: VerificationRunResponse | null;
  onStarted: (run: VerificationRunResponse) => void;
}) {
  const [open, setOpen] = useState(false);
  const [testCommand, setTestCommand] = useState("");
  const [observePort, setObservePort] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const active = verification !== null && ACTIVE_STATUSES.has(verification.status);
  const showVerify = canVerify && !active;

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const payload: VerifyAgentRunPayload = {};
    if (testCommand.trim()) payload.test_command = testCommand.trim();
    if (observePort.trim()) payload.observe_port = Number(observePort);

    setBusy(true);
    setError(null);
    try {
      onStarted(await verifyAgentRun(workspaceId, runId, payload));
      setOpen(false);
      setTestCommand("");
      setObservePort("");
    } catch (err) {
      setError(err instanceof Error && err.message ? err.message : "Unable to start verification");
    } finally {
      setBusy(false);
    }
  }

  if (!verification && !showVerify) return null;

  const inputClass =
    "mt-1 w-full rounded-md border border-line bg-white px-2 py-1.5 text-sm focus:border-primary focus:outline-none";
  const labelClass = "block text-xs font-semibold text-ink/70";

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {verification && <VerificationChip verification={verification} />}
        {showVerify && !open && (
          <button
            type="button"
            onClick={() => setOpen(true)}
            className="inline-flex items-center gap-1.5 text-xs font-medium text-primary transition hover:text-primary-active"
          >
            <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
            Verify
          </button>
        )}
      </div>

      {showVerify && open && (
        <form
          onSubmit={handleSubmit}
          aria-label="Verify this run"
          className="max-w-md space-y-3 rounded-md border border-line bg-panel p-3"
        >
          <label className={labelClass}>
            Test command (optional)
            <input
              className={`${inputClass} font-mono`}
              value={testCommand}
              onChange={(e) => setTestCommand(e.target.value)}
              placeholder="Empty uses the workspace's test command"
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
          {error && (
            <p role="alert" className="text-xs text-danger">
              {error}
            </p>
          )}
          <div className="flex items-center gap-2">
            <button
              type="submit"
              disabled={busy}
              className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-sm font-semibold text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:opacity-60"
            >
              {busy ? (
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              ) : (
                <Play className="h-4 w-4" aria-hidden="true" />
              )}
              Start
            </button>
            <button
              type="button"
              onClick={() => {
                setOpen(false);
                setError(null);
              }}
              className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-white px-3 py-1.5 text-sm font-semibold text-ink transition hover:bg-panel"
            >
              <X className="h-4 w-4" aria-hidden="true" />
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
