import { describe, expect, it } from "vitest";
import type { AgentEventResponse } from "../../lib/api";
import {
  detectBlocked,
  getDiff,
  getGitStatus,
  getSearchMatches,
  isSearchTruncated,
} from "./toolMetadata";

function result(
  metadata: Record<string, unknown>,
  content = "",
): AgentEventResponse {
  return {
    id: "evt",
    agent_run_id: "run",
    event_type: "tool_result",
    content,
    metadata,
    tool_call_id: "tc",
    created_at: new Date().toISOString(),
  };
}

describe("getDiff", () => {
  it("returns the diff string", () => {
    expect(getDiff(result({ diff: "@@\n+x" }))).toBe("@@\n+x");
  });
  it("returns null when absent or empty", () => {
    expect(getDiff(result({}))).toBeNull();
    expect(getDiff(result({ diff: "" }))).toBeNull();
    expect(getDiff(null)).toBeNull();
  });
});

describe("getSearchMatches", () => {
  it("parses an array of hits", () => {
    const hits = getSearchMatches(
      result({ matches: [{ path: "a.py", line_number: 2, text: "t" }] }),
    );
    expect(hits).toEqual([{ path: "a.py", line_number: 2, text: "t" }]);
  });
  it("parses a JSON-string payload", () => {
    const hits = getSearchMatches(
      result({ matches: JSON.stringify([{ path: "b.py", line_number: 1, text: "" }]) }),
    );
    expect(hits).toEqual([{ path: "b.py", line_number: 1, text: "" }]);
  });
  it("returns an empty array for no matches", () => {
    expect(getSearchMatches(result({ matches: [] }))).toEqual([]);
  });
  it("returns null for unknown / malformed shapes", () => {
    expect(getSearchMatches(result({}))).toBeNull();
    expect(getSearchMatches(result({ matches: "not json {" }))).toBeNull();
    expect(getSearchMatches(result({ matches: 42 }))).toBeNull();
  });
  it("skips malformed items", () => {
    const hits = getSearchMatches(
      result({ matches: [{ path: "ok.py", line_number: 5, text: "y" }, { nope: 1 }] }),
    );
    expect(hits).toEqual([{ path: "ok.py", line_number: 5, text: "y" }]);
  });
});

describe("getGitStatus", () => {
  it("parses branch, cleanliness, and files", () => {
    const status = getGitStatus(
      result({ branch: "main", is_clean: false, files: [{ path: "f", status: "M" }] }),
    );
    expect(status).toEqual({
      branch: "main",
      is_clean: false,
      files: [{ path: "f", status: "M" }],
    });
  });
  it("returns null without a files array", () => {
    expect(getGitStatus(result({ branch: "main" }))).toBeNull();
  });
});

describe("isSearchTruncated", () => {
  it("reflects the truncated flag", () => {
    expect(isSearchTruncated(result({ truncated: true }))).toBe(true);
    expect(isSearchTruncated(result({}))).toBe(false);
  });
});

describe("detectBlocked", () => {
  it("treats ok:false with no exit_code and a policy message as blocked", () => {
    expect(detectBlocked(result({ ok: false }, "Command is not allowed"))).toBe(true);
  });
  it("treats explicit flags as blocked", () => {
    expect(detectBlocked(result({ blocked: true }))).toBe(true);
    expect(detectBlocked(result({ rejected: true }))).toBe(true);
  });
  it("does NOT treat an ordinary non-zero exit as blocked", () => {
    expect(detectBlocked(result({ exit_code: 1 }, "Command is not allowed"))).toBe(false);
    expect(detectBlocked(result({ exit_code: 2 }, "boom"))).toBe(false);
  });
  it("is false for ordinary results and null", () => {
    expect(detectBlocked(result({}, "fine"))).toBe(false);
    expect(detectBlocked(null)).toBe(false);
  });
});
