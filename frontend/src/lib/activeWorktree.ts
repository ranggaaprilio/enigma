import { useCallback, useEffect, useState } from "react";

// The active worktree drives which branch the agent and terminal target. Dashboard views are
// rendered one route at a time, so the selection is persisted in localStorage (keyed by
// workspace) and broadcast via a window event so any mounted view stays in sync.

export type ActiveWorktree = {
  id: string;
  branch: string;
};

const EVENT_NAME = "enigma:active-worktree";

function storageKey(workspaceId: string): string {
  return `enigma.activeWorktree.${workspaceId}`;
}

export function getActiveWorktree(workspaceId: string): ActiveWorktree | null {
  try {
    const raw = window.localStorage.getItem(storageKey(workspaceId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as ActiveWorktree;
    return parsed && typeof parsed.id === "string" ? parsed : null;
  } catch {
    return null;
  }
}

export function setActiveWorktree(workspaceId: string, value: ActiveWorktree | null): void {
  try {
    if (value) {
      window.localStorage.setItem(storageKey(workspaceId), JSON.stringify(value));
    } else {
      window.localStorage.removeItem(storageKey(workspaceId));
    }
  } catch {
    // Ignore storage failures (e.g. private mode); the event below still updates live state.
  }
  window.dispatchEvent(
    new CustomEvent<{ workspaceId: string; value: ActiveWorktree | null }>(EVENT_NAME, {
      detail: { workspaceId, value },
    }),
  );
}

export function useActiveWorktree(
  workspaceId: string,
): [ActiveWorktree | null, (value: ActiveWorktree | null) => void] {
  const [value, setValue] = useState<ActiveWorktree | null>(() =>
    getActiveWorktree(workspaceId),
  );

  useEffect(() => {
    setValue(getActiveWorktree(workspaceId));
  }, [workspaceId]);

  useEffect(() => {
    function handler(event: Event) {
      const detail = (event as CustomEvent<{ workspaceId: string; value: ActiveWorktree | null }>)
        .detail;
      if (detail?.workspaceId === workspaceId) {
        setValue(detail.value);
      }
    }
    window.addEventListener(EVENT_NAME, handler);
    return () => window.removeEventListener(EVENT_NAME, handler);
  }, [workspaceId]);

  const update = useCallback(
    (next: ActiveWorktree | null) => {
      setActiveWorktree(workspaceId, next);
      setValue(next);
    },
    [workspaceId],
  );

  return [value, update];
}
