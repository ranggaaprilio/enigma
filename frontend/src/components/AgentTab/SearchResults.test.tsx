import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SearchResults, groupByFile } from "./SearchResults";

describe("groupByFile", () => {
  it("groups hits by file in first-seen order", () => {
    const groups = groupByFile([
      { path: "b.py", line_number: 1, text: "x" },
      { path: "a.py", line_number: 2, text: "y" },
      { path: "b.py", line_number: 3, text: "z" },
    ]);
    expect(groups.map((g) => g.path)).toEqual(["b.py", "a.py"]);
    expect(groups[0].hits).toHaveLength(2);
    expect(groups[1].hits).toHaveLength(1);
  });
});

describe("SearchResults", () => {
  it("renders the empty state", () => {
    render(<SearchResults matches={[]} />);
    expect(screen.getByText("No matches found.")).toBeInTheDocument();
  });

  it("renders grouped hits with line numbers and snippets", () => {
    render(
      <SearchResults
        matches={[{ path: "src/a.py", line_number: 12, text: "needle" }]}
      />,
    );
    expect(screen.getByText("src/a.py")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("needle")).toBeInTheDocument();
  });

  it("shows a truncation note when truncated", () => {
    render(
      <SearchResults
        matches={[{ path: "a.py", line_number: 1, text: "t" }]}
        truncated
      />,
    );
    expect(screen.getByText("More matches were truncated.")).toBeInTheDocument();
  });
});
