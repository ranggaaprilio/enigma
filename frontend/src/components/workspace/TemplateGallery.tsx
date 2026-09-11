import { GitBranch, LayoutTemplate } from "lucide-react";

import type { WorkspaceTemplateResponse } from "../../lib/api";

export function TemplateGallery({
  templates,
  loading,
  error,
  selectedId,
  onSelect,
  onRetry,
}: {
  templates: WorkspaceTemplateResponse[] | null;
  loading: boolean;
  error: string | null;
  selectedId: string | null;
  onSelect: (template: WorkspaceTemplateResponse) => void;
  onRetry?: () => void;
}) {
  if (loading) {
    return (
      <div className="grid gap-4 sm:grid-cols-2" aria-busy="true" aria-label="Loading templates">
        {[0, 1, 2].map((index) => (
          <div
            key={index}
            className="h-32 animate-pulse rounded-xl border border-line bg-panel"
            aria-hidden="true"
          />
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-md border border-danger/30 bg-[#fff5f2] p-4 text-sm text-danger">
        <p>{error}</p>
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="mt-2 rounded-md border border-danger/40 px-3 py-1 text-xs font-semibold"
          >
            Retry
          </button>
        )}
      </div>
    );
  }

  const items = [...(templates ?? [])].sort(
    (a, b) => a.sort_order - b.sort_order || a.name.localeCompare(b.name),
  );

  if (items.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-line bg-panel p-6 text-center text-sm text-body">
        No templates are available yet. Start blank or connect a repository instead.
      </div>
    );
  }

  return (
    <div role="radiogroup" aria-label="Templates" className="grid gap-4 sm:grid-cols-2">
      {items.map((template) => {
        const selected = template.id === selectedId;
        return (
          <button
            key={template.id}
            type="button"
            role="radio"
            aria-checked={selected}
            onClick={() => onSelect(template)}
            className={`group flex flex-col rounded-xl border bg-panel p-4 text-left transition hover:border-primary ${
              selected ? "border-ink ring-1 ring-ink" : "border-line"
            }`}
          >
            <div className="flex items-start gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-canvas text-body group-hover:text-primary">
                <LayoutTemplate className="h-5 w-5" aria-hidden="true" />
              </div>
              <div className="min-w-0">
                <h4 className="truncate font-semibold text-ink">{template.name}</h4>
                <p className="mt-1 line-clamp-2 text-sm text-body">{template.description}</p>
              </div>
            </div>
            {template.tags.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {template.tags.map((tag) => (
                  <span
                    key={tag}
                    className="rounded-full bg-line px-2 py-0.5 text-xs text-body"
                  >
                    {tag}
                  </span>
                ))}
              </div>
            )}
            <p className="mt-3 flex items-center gap-1.5 text-xs text-muted">
              <GitBranch className="h-3.5 w-3.5" aria-hidden="true" />
              {template.default_branch}
            </p>
          </button>
        );
      })}
    </div>
  );
}
