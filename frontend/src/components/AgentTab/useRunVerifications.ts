import { useCallback, useEffect, useState } from "react";

import {
  listVerificationRuns,
  type VerificationRunResponse,
  type VerificationRunStatus,
} from "../../lib/api";

/** Verification runs in these statuses are still moving. */
export const ACTIVE_STATUSES: ReadonlySet<VerificationRunStatus> = new Set([
  "planning",
  "testing",
  "fixing",
  "observing",
]);
const POLL_MS = 3_000;
/** The API's maximum page; older verifications simply show no chip. */
const LIST_LIMIT = 100;

type ByRun = Map<string, VerificationRunResponse>;

/** Keep the newest verification per agent run. */
function mergeLatest(current: ByRun, runs: Iterable<VerificationRunResponse>): ByRun {
  const next = new Map(current);
  for (const run of runs) {
    if (!run.source_agent_run_id) continue;
    const known = next.get(run.source_agent_run_id);
    if (!known || known.id === run.id || run.created_at >= known.created_at) {
      next.set(run.source_agent_run_id, run);
    }
  }
  return next;
}

/**
 * The latest verification of each agent run in the workspace, from one list call. Polls while
 * any of them is still moving.
 */
export function useRunVerifications(workspaceId: string, enabled: boolean) {
  const [byRun, setByRun] = useState<ByRun>(() => new Map());
  const [pollRetry, setPollRetry] = useState(0);

  const refresh = useCallback(async () => {
    // Index outside the state update, so a malformed response rejects here, not in render.
    const fetched = mergeLatest(new Map(), await listVerificationRuns(workspaceId, LIST_LIMIT));
    setByRun((current) => mergeLatest(current, fetched.values()));
  }, [workspaceId]);

  useEffect(() => {
    if (!enabled) return;
    refresh().catch(() => undefined);
  }, [enabled, refresh]);

  const anyActive = [...byRun.values()].some((run) => ACTIVE_STATUSES.has(run.status));
  useEffect(() => {
    if (!enabled || !anyActive) return;
    const timer = window.setTimeout(() => {
      refresh().catch(() => setPollRetry((n) => n + 1));
    }, POLL_MS);
    return () => window.clearTimeout(timer);
    // `byRun` re-arms the timer after every refresh, even when nothing changed.
  }, [enabled, anyActive, byRun, pollRetry, refresh]);

  const add = useCallback((run: VerificationRunResponse) => {
    setByRun((current) => mergeLatest(current, [run]));
  }, []);

  return { byRun, refresh, add };
}
