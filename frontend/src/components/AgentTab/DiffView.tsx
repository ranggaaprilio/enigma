// Lightweight unified-diff renderer. No external dependency — classifies each
// line and color-codes it with the existing palette. The page body never
// scrolls horizontally: the outer container scrolls, the inner block is min-w-max.

export type DiffLineKind = "add" | "del" | "hunk" | "meta" | "context";

export interface DiffLine {
  kind: DiffLineKind;
  text: string;
}

export function parseDiff(diff: string): DiffLine[] {
  return diff.split("\n").map((line) => {
    // File headers must be matched before the generic +/- branch.
    if (
      line.startsWith("+++") ||
      line.startsWith("---") ||
      line.startsWith("diff ") ||
      line.startsWith("index ")
    ) {
      return { kind: "meta", text: line };
    }
    if (line.startsWith("@@")) return { kind: "hunk", text: line };
    if (line.startsWith("+")) return { kind: "add", text: line };
    if (line.startsWith("-")) return { kind: "del", text: line };
    return { kind: "context", text: line };
  });
}

const LINE_STYLES: Record<DiffLineKind, string> = {
  add: "bg-signal/10 text-ink",
  del: "bg-danger/10 text-ink",
  hunk: "bg-primary/10 text-primary",
  meta: "text-muted",
  context: "text-body",
};

export function DiffView({ diff }: { diff: string }) {
  const lines = parseDiff(diff);
  const isEmpty =
    lines.length === 0 || (lines.length === 1 && lines[0].text === "");

  if (isEmpty) {
    return <p className="text-xs text-muted">No diff to display.</p>;
  }

  return (
    <div className="max-h-72 overflow-auto rounded border border-line bg-white font-mono text-xs leading-5">
      <div className="min-w-max">
        {lines.map((line, i) => (
          <div key={i} className={`whitespace-pre px-2 ${LINE_STYLES[line.kind]}`}>
            {line.text || " "}
          </div>
        ))}
      </div>
    </div>
  );
}
