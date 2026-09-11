import type { AgentEventResponse } from "../../lib/api";

// ---------------------------------------------------------------------------
// Shapes the new editing tools put on tool_result metadata. These are
// advisory — metadata stays a permissive Record<string, unknown> and every
// helper coerces defensively, returning null on unknown shapes so callers can
// fall back to generic rendering.
// ---------------------------------------------------------------------------

export interface SearchHit {
  path: string;
  line_number: number;
  text: string;
}

export interface GitStatusFile {
  path: string;
  status: string;
}

export interface GitStatusShape {
  branch: string | null;
  is_clean: boolean;
  files: GitStatusFile[];
}

function metaOf(result: AgentEventResponse | null): Record<string, unknown> {
  return (result?.metadata ?? {}) as Record<string, unknown>;
}

/** A metadata value may arrive as a JSON string or already parsed. */
function asValue(value: unknown): unknown {
  if (typeof value === "string") {
    try {
      return JSON.parse(value);
    } catch {
      return null;
    }
  }
  return value;
}

/** Unified-diff string for apply_patch / git_diff results, if present. */
export function getDiff(result: AgentEventResponse | null): string | null {
  const value = metaOf(result).diff;
  return typeof value === "string" && value.length > 0 ? value : null;
}

/** Structured search results for search_code. Returns [] for "no matches". */
export function getSearchMatches(
  result: AgentEventResponse | null,
): SearchHit[] | null {
  const raw = asValue(metaOf(result).matches);
  if (!Array.isArray(raw)) return null;
  const hits: SearchHit[] = [];
  for (const item of raw) {
    if (item && typeof item === "object") {
      const rec = item as Record<string, unknown>;
      if (typeof rec.path === "string" && typeof rec.line_number === "number") {
        hits.push({
          path: rec.path,
          line_number: rec.line_number,
          text: typeof rec.text === "string" ? rec.text : "",
        });
      }
    }
  }
  return hits;
}

/** Structured git_status payload, if present. */
export function getGitStatus(
  result: AgentEventResponse | null,
): GitStatusShape | null {
  const rawFiles = asValue(metaOf(result).files);
  if (!Array.isArray(rawFiles)) return null;
  const files: GitStatusFile[] = [];
  for (const item of rawFiles) {
    if (item && typeof item === "object") {
      const rec = item as Record<string, unknown>;
      if (typeof rec.path === "string") {
        files.push({
          path: rec.path,
          status: typeof rec.status === "string" ? rec.status : "",
        });
      }
    }
  }
  const meta = metaOf(result);
  return {
    branch: typeof meta.branch === "string" ? meta.branch : null,
    is_clean: typeof meta.is_clean === "boolean" ? meta.is_clean : files.length === 0,
    files,
  };
}

/** Whether a search_code result reported more matches than returned. */
export function isSearchTruncated(result: AgentEventResponse | null): boolean {
  return metaOf(result).truncated === true;
}

/**
 * Distinguish a run_command *policy rejection* from an ordinary non-zero exit.
 * A present numeric exit_code means the command actually ran, so it is NOT a
 * block. Policy rejections carry no exit_code and surface a known message.
 */
export function detectBlocked(result: AgentEventResponse | null): boolean {
  if (!result) return false;
  const meta = metaOf(result);
  if (meta.blocked === true || meta.rejected === true || meta.policy_violation === true) {
    return true;
  }
  if (typeof meta.exit_code === "number") return false;
  const content = (result.content || "").toLowerCase();
  return (
    content.includes("command is not allowed") ||
    content.includes("shell control operators are not allowed") ||
    content.includes("command could not be parsed") ||
    content.includes("command cannot be empty")
  );
}
