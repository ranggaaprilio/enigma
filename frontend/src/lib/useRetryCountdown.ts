import { useCallback, useEffect, useState } from "react";

import { ApiError } from "./api";

/**
 * Turns a rate-limited / at-capacity response (429 or 503 with `Retry-After`) into a
 * countdown. Callers disable their action while `waiting` and show `notice`. There is no
 * auto-retry: the actions it guards start paid runs, so the user re-submits deliberately.
 */
export function useRetryCountdown() {
  const [secondsLeft, setSecondsLeft] = useState(0);
  const [reason, setReason] = useState("");

  useEffect(() => {
    if (secondsLeft <= 0) return;
    const timer = setTimeout(() => setSecondsLeft((seconds) => seconds - 1), 1000);
    return () => clearTimeout(timer);
  }, [secondsLeft]);

  /** Starts the countdown if `err` is retryable; returns whether it did. */
  const arm = useCallback((err: unknown): boolean => {
    if (
      err instanceof ApiError &&
      (err.status === 429 || err.status === 503) &&
      err.retryAfter !== undefined
    ) {
      setReason(err.message);
      setSecondsLeft(Math.ceil(err.retryAfter));
      return true;
    }
    return false;
  }, []);

  const waiting = secondsLeft > 0;
  return {
    waiting,
    notice: waiting ? `${reason} Retry in ${secondsLeft}s.` : null,
    arm,
  };
}
