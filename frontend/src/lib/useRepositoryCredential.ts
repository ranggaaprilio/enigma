import { useCallback, useEffect, useState } from "react";

import { ApiError, getRepositoryCredential, type RepositoryCredentialResponse } from "./api";

function isCredential(value: unknown): value is RepositoryCredentialResponse {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { provider?: unknown }).provider === "string" &&
    typeof (value as { auth_type?: unknown }).auth_type === "string"
  );
}

/**
 * The workspace's git credential summary (provider, auth type, public key). A 404 means none is
 * configured and resolves to `null` without an error. `refreshKey` re-fetches when it changes.
 */
export function useRepositoryCredential(workspaceId: string, refreshKey?: unknown) {
  const [credential, setCredential] = useState<RepositoryCredentialResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      try {
        const data = await getRepositoryCredential(workspaceId, signal);
        setCredential(isCredential(data) ? data : null);
        setError(null);
      } catch (caughtError) {
        if (caughtError instanceof DOMException && caughtError.name === "AbortError") return;
        if (caughtError instanceof ApiError && caughtError.status === 404) {
          setCredential(null);
          setError(null);
        } else {
          setError(
            caughtError instanceof ApiError
              ? caughtError.message
              : "Unable to load repository credential.",
          );
        }
      } finally {
        setLoading(false);
      }
    },
    [workspaceId],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, refreshKey]);

  return { credential, loading, error, refresh: () => load() };
}
