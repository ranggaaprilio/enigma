import { useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Wrench,
  CheckCircle2,
  XCircle,
  Loader2,
  Unplug,
  FileEdit,
} from "lucide-react";
import type { AgentEventResponse, AgentFileChangeResponse } from "../../lib/api";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ToolStatus = "running" | "completed" | "error";

export interface PairedToolEvent {
  toolCallId: string | null;
  call: AgentEventResponse | null;
  result: AgentEventResponse | null;
}

export interface ToolCardProps {
  event: PairedToolEvent;
  fileChanges?: AgentFileChangeResponse[];
  streamError?: string | null;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Derive display status from paired events. */
function deriveStatus(
  event: PairedToolEvent,
  streamError?: string | null,
): ToolStatus {
  if (streamError) return "error";
  if (event.result) {
    const meta = event.result.metadata ?? {};
    if (meta.exit_code !== undefined && meta.exit_code !== 0) return "error";
    return "completed";
  }
  return "running";
}

/** Extract a human-readable tool name from a tool_call event. */
function toolName(call: AgentEventResponse | null): string {
  if (!call) return "Unknown tool";
  const meta = call.metadata ?? {};
  if (typeof meta.tool_name === "string") return meta.tool_name;
  if (typeof meta.name === "string") return meta.name;
  // Fallback: use content first line
  const firstLine = call.content.split("\n")[0]?.trim();
  return firstLine || "Tool call";
}

/** Extract arguments string from a tool_call event. */
function toolArguments(call: AgentEventResponse | null): string | null {
  if (!call) return null;
  const meta = call.metadata ?? {};
  if (typeof meta.arguments === "string") return meta.arguments;
  if (typeof meta.args === "string") return meta.args;
  if (meta.arguments && typeof meta.arguments === "object")
    return JSON.stringify(meta.arguments, null, 2);
  if (meta.args && typeof meta.args === "object")
    return JSON.stringify(meta.args, null, 2);
  return null;
}

/** Extract output from a tool_result event. */
function toolOutput(result: AgentEventResponse | null): string | null {
  if (!result) return null;
  const meta = result.metadata ?? {};
  if (typeof meta.output === "string") return meta.output;
  if (typeof meta.result === "string") return meta.result;
  return result.content || null;
}

/** Extract exit code from a tool_result event. */
function exitCode(result: AgentEventResponse | null): number | null {
  if (!result) return null;
  const meta = result.metadata ?? {};
  if (typeof meta.exit_code === "number") return meta.exit_code;
  return null;
}

// ---------------------------------------------------------------------------
// Status badge
// ---------------------------------------------------------------------------

const STATUS_STYLES: Record<ToolStatus, string> = {
  running: "bg-timeline-thinking/15 text-timeline-thinking",
  completed: "bg-signal/15 text-signal",
  error: "bg-danger/15 text-danger",
};

const STATUS_LABELS: Record<ToolStatus, string> = {
  running: "Running",
  completed: "Completed",
  error: "Error",
};

function StatusBadge({ status }: { status: ToolStatus }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs font-medium ${STATUS_STYLES[status]}`}
    >
      {status === "running" && <Loader2 className="h-3 w-3 animate-spin" />}
      {status === "completed" && <CheckCircle2 className="h-3 w-3" />}
      {status === "error" && <XCircle className="h-3 w-3" />}
      {STATUS_LABELS[status]}
    </span>
  );
}

// ---------------------------------------------------------------------------
// ToolCard component
// ---------------------------------------------------------------------------

export function ToolCard({ event, fileChanges, streamError }: ToolCardProps) {
  const [expanded, setExpanded] = useState(false);
  const status = deriveStatus(event, streamError);
  const name = toolName(event.call ?? event.result);
  const args = toolArguments(event.call);
  const output = toolOutput(event.result);
  const code = exitCode(event.result);
  const changes = fileChanges ?? [];

  return (
    <div className="rounded-lg border border-line bg-panel">
      {/* Collapsed header */}
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-line-soft"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        {expanded ? (
          <ChevronDown className="h-4 w-4 shrink-0 text-muted" />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0 text-muted" />
        )}
        <Wrench className="h-4 w-4 shrink-0 text-muted-soft" />
        <span className="min-w-0 truncate text-sm font-medium text-ink">
          {name}
        </span>
        <span className="ml-auto shrink-0">
          <StatusBadge status={status} />
        </span>
      </button>

      {/* Stream disconnect error — always visible, separate from tool errors */}
      {streamError && (
        <div className="border-t border-line px-3 py-2">
          <div className="rounded border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">
            <Unplug className="mr-1 inline h-3 w-3" />
            Stream disconnected: {streamError}
          </div>
        </div>
      )}

      {/* Expanded detail */}
      {expanded && (
        <div className="border-t border-line px-3 py-2 text-sm text-body">
          {/* Arguments */}
          {args && (
            <div className="mb-2">
              <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">
                Arguments
              </p>
              <pre className="max-h-40 overflow-auto rounded border border-line bg-white p-2 text-xs text-ink">
                {args}
              </pre>
            </div>
          )}

          {/* Output */}
          {output && (
            <div className="mb-2">
              <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">
                Output
              </p>
              <pre className="max-h-40 overflow-auto rounded border border-line bg-white p-2 text-xs text-ink">
                {output}
              </pre>
            </div>
          )}

          {/* Exit code */}
          {code !== null && (
            <div className="mb-2">
              <span className="text-xs font-medium uppercase tracking-wide text-muted">
                Exit code:
              </span>{" "}
              <span
                className={
                  code === 0 ? "text-signal" : "text-danger"
                }
              >
                {code}
              </span>
            </div>
          )}

          {/* Changed files */}
          {changes.length > 0 && (
            <div>
              <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">
                Changed files
              </p>
              <ul className="space-y-1">
                {changes.map((fc) => (
                  <li
                    key={fc.id}
                    className="flex items-center gap-1.5 text-xs text-ink"
                  >
                    <FileEdit className="h-3 w-3 shrink-0 text-muted-soft" />
                    <span className="min-w-0 truncate font-mono">{fc.path}</span>
                    <span className="ml-auto shrink-0 rounded bg-line px-1 text-[10px] text-muted">
                      {fc.status}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Pairing utility — exported for use in parent components and tests
// ---------------------------------------------------------------------------

export function pairToolEvents(
  events: AgentEventResponse[],
): PairedToolEvent[] {
  const byId = new Map<string, PairedToolEvent>();
  const unpaired: PairedToolEvent[] = [];

  for (const ev of events) {
    if (ev.event_type !== "tool_call" && ev.event_type !== "tool_result") {
      continue;
    }

    const metadataId = ev.metadata?.tool_call_id;
    const id =
      ev.tool_call_id ??
      (typeof metadataId === "string" ? metadataId : null);

    // No tool_call_id → ungrouped, each gets its own card
    if (!id) {
      if (ev.event_type === "tool_call") {
        unpaired.push({ toolCallId: null, call: ev, result: null });
      } else {
        unpaired.push({ toolCallId: null, call: null, result: ev });
      }
      continue;
    }

    let pair = byId.get(id);
    if (!pair) {
      pair = { toolCallId: id, call: null, result: null };
      byId.set(id, pair);
    }

    if (ev.event_type === "tool_call") {
      pair.call = ev;
    } else {
      pair.result = ev;
    }
  }

  // Preserve insertion order
  const ordered: PairedToolEvent[] = [];
  const seen = new Set<string>();

  for (const ev of events) {
    if (ev.event_type !== "tool_call" && ev.event_type !== "tool_result")
      continue;
    const metadataId = ev.metadata?.tool_call_id;
    const id =
      ev.tool_call_id ??
      (typeof metadataId === "string" ? metadataId : null);
    if (!id) continue;
    if (seen.has(id)) continue;
    seen.add(id);
    ordered.push(byId.get(id)!);
  }

  return [...ordered, ...unpaired];
}
