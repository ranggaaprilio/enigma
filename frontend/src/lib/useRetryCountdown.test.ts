import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./api";
import { useRetryCountdown } from "./useRetryCountdown";

describe("useRetryCountdown", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("counts down a 429's Retry-After and then clears", () => {
    const { result } = renderHook(() => useRetryCountdown());

    let armed = false;
    act(() => {
      armed = result.current.arm(new ApiError("Too many requests. Please slow down.", 429, 2));
    });

    expect(armed).toBe(true);
    expect(result.current.waiting).toBe(true);
    expect(result.current.notice).toBe("Too many requests. Please slow down. Retry in 2s.");

    act(() => vi.advanceTimersByTime(1000));
    expect(result.current.notice).toBe("Too many requests. Please slow down. Retry in 1s.");

    act(() => vi.advanceTimersByTime(1000));
    expect(result.current.waiting).toBe(false);
    expect(result.current.notice).toBeNull();
  });

  it("arms on a 503 that carries Retry-After", () => {
    const { result } = renderHook(() => useRetryCountdown());

    act(() => {
      result.current.arm(new ApiError("The run queue is backed up.", 503, 15));
    });

    expect(result.current.notice).toBe("The run queue is backed up. Retry in 15s.");
  });

  it("ignores errors that are not retryable", () => {
    const { result } = renderHook(() => useRetryCountdown());

    let armed = true;
    act(() => {
      armed = result.current.arm(new ApiError("busy", 409));
    });
    expect(armed).toBe(false);
    act(() => {
      armed = result.current.arm(new ApiError("down", 503));
    });
    expect(armed).toBe(false);
    act(() => {
      armed = result.current.arm(new Error("boom"));
    });

    expect(armed).toBe(false);
    expect(result.current.waiting).toBe(false);
  });
});
