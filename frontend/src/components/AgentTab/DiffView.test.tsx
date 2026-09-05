import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DiffView, parseDiff } from "./DiffView";

describe("parseDiff", () => {
  it("classifies each line kind", () => {
    const diff = [
      "diff --git a/x b/x",
      "index abc..def 100644",
      "--- a/x",
      "+++ b/x",
      "@@ -1,2 +1,2 @@",
      " context",
      "-removed",
      "+added",
    ].join("\n");

    const kinds = parseDiff(diff).map((line) => line.kind);
    expect(kinds).toEqual([
      "meta",
      "meta",
      "meta",
      "meta",
      "hunk",
      "context",
      "del",
      "add",
    ]);
  });

  it("classifies +++/--- headers as meta, not add/del", () => {
    const parsed = parseDiff("--- a/x\n+++ b/x");
    expect(parsed[0].kind).toBe("meta");
    expect(parsed[1].kind).toBe("meta");
  });
});

describe("DiffView", () => {
  it("renders added and removed lines", () => {
    render(<DiffView diff={"@@ -1 +1 @@\n-old\n+new"} />);
    expect(screen.getByText("+new")).toBeInTheDocument();
    expect(screen.getByText("-old")).toBeInTheDocument();
  });

  it("shows a placeholder for an empty diff", () => {
    render(<DiffView diff={""} />);
    expect(screen.getByText("No diff to display.")).toBeInTheDocument();
  });
});
