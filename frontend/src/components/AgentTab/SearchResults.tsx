import type { SearchHit } from "./toolMetadata";

// Collapsible search_code results, grouped by file in first-seen order. File
// path + line is rendered as a clickable-looking anchor (navigation is a future
// enhancement; an optional onOpen callback is wired through for it).

export interface FileGroup {
  path: string;
  hits: SearchHit[];
}

export function groupByFile(matches: SearchHit[]): FileGroup[] {
  const order: string[] = [];
  const byPath = new Map<string, SearchHit[]>();
  for (const match of matches) {
    let bucket = byPath.get(match.path);
    if (!bucket) {
      bucket = [];
      byPath.set(match.path, bucket);
      order.push(match.path);
    }
    bucket.push(match);
  }
  return order.map((path) => ({ path, hits: byPath.get(path)! }));
}

interface SearchResultsProps {
  matches: SearchHit[];
  truncated?: boolean;
  onOpen?: (path: string, line: number) => void;
}

export function SearchResults({ matches, truncated, onOpen }: SearchResultsProps) {
  if (matches.length === 0) {
    return <p className="text-xs text-muted">No matches found.</p>;
  }

  const groups = groupByFile(matches);

  return (
    <div className="space-y-2">
      {groups.map((group) => (
        <details
          key={group.path}
          open
          className="rounded border border-line bg-white"
        >
          <summary className="flex cursor-pointer items-center gap-2 px-2 py-1.5 text-xs font-medium text-ink">
            <span className="min-w-0 truncate font-mono">{group.path}</span>
            <span className="ml-auto shrink-0 rounded bg-line px-1 text-[10px] text-muted">
              {group.hits.length}
            </span>
          </summary>
          <ul className="border-t border-line">
            {group.hits.map((hit, i) => (
              <li
                key={i}
                className="flex gap-2 overflow-hidden px-2 py-1 font-mono text-xs"
              >
                <button
                  type="button"
                  className="shrink-0 select-none text-muted hover:text-primary"
                  onClick={() => onOpen?.(hit.path, hit.line_number)}
                  title={`${hit.path}:${hit.line_number}`}
                >
                  {hit.line_number}
                </button>
                <span className="min-w-0 truncate text-ink">{hit.text}</span>
              </li>
            ))}
          </ul>
        </details>
      ))}
      {truncated && (
        <p className="text-[11px] text-muted">More matches were truncated.</p>
      )}
    </div>
  );
}
