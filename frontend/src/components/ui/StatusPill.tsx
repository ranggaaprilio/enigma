import { CheckCircle2, Circle, Loader2, TerminalSquare, XCircle } from "lucide-react";

/** Covers both the command-run vocabulary and the workspace-service vocabulary (Epic 11) so
 * one pill renders either consistently. An unrecognised value falls back to a neutral dot. */
export type StatusPillStatus =
  | "idle"
  | "running"
  | "completed"
  | "failed"
  | "error"
  | "installing"
  | "starting"
  | "stopped"
  | "exited";

const TONE_CLASS: Partial<Record<StatusPillStatus, string>> = {
  idle: "bg-white text-muted ring-1 ring-line",
  stopped: "bg-white text-muted ring-1 ring-line",
  installing: "bg-line text-warn",
  starting: "bg-line text-warn",
  running: "bg-line text-signal",
  completed: "bg-line text-signal",
  failed: "bg-[#fff5f2] text-danger",
  error: "bg-[#fff5f2] text-danger",
  exited: "bg-[#fff5f2] text-danger",
};

const ICON: Partial<Record<StatusPillStatus, typeof CheckCircle2>> = {
  completed: CheckCircle2,
  idle: TerminalSquare,
  stopped: TerminalSquare,
  installing: Loader2,
  starting: Loader2,
  failed: XCircle,
  error: XCircle,
  exited: XCircle,
};

const SPINNING: Partial<Record<StatusPillStatus, boolean>> = {
  installing: true,
  starting: true,
};

export function StatusPill({ status, label }: { status: string; label?: string }) {
  const key = status as StatusPillStatus;
  const toneClass = TONE_CLASS[key] ?? "bg-white text-muted ring-1 ring-line";
  const Icon = ICON[key] ?? Circle;
  return (
    <span
      className={`inline-flex items-center gap-2 rounded-full px-3 py-1 text-sm font-semibold capitalize ${toneClass}`}
    >
      <Icon className={`h-4 w-4 ${SPINNING[key] ? "animate-spin" : ""}`} aria-hidden="true" />
      {label ?? status}
    </span>
  );
}
