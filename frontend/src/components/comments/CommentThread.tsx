import { useCallback, useEffect, useState } from "react";
import { Check, MessageSquare } from "lucide-react";

import {
  ApiError,
  createComment,
  listComments,
  resolveComment,
  type CommentResponse,
  type CommentTargetType,
} from "../../lib/api";

interface CommentThreadProps {
  targetType: CommentTargetType;
  targetId: string;
}

/**
 * A comment thread for a single target (run, spec phase, or PR) — list, add, resolve.
 * Self-contained and reusable across all three target types via the `targetType` prop.
 */
export function CommentThread({ targetType, targetId }: CommentThreadProps) {
  const [comments, setComments] = useState<CommentResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const data = await listComments(targetType, targetId, signal);
      setComments(data);
    } catch (err) {
      if (err instanceof ApiError) setError(err.message);
    }
  }, [targetType, targetId]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    // Drop the previous target's comments so a slow (or failing) fetch never shows them
    // under the newly selected target.
    setComments([]);
    load(controller.signal).finally(() => setLoading(false));
    return () => controller.abort();
  }, [load]);

  const handleSubmit = useCallback(async () => {
    const trimmed = draft.trim();
    if (!trimmed || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const created = await createComment(targetType, targetId, trimmed);
      setComments((prev) => [...prev, created]);
      setDraft("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to post comment");
    } finally {
      setSubmitting(false);
    }
  }, [draft, submitting, targetType, targetId]);

  const handleResolve = useCallback(async (commentId: string) => {
    try {
      const updated = await resolveComment(commentId);
      setComments((prev) => prev.map((c) => (c.id === updated.id ? updated : c)));
    } catch {
      // Leave the comment as-is; the user can retry.
    }
  }, []);

  return (
    <div className="rounded-md border border-line bg-white p-3" data-testid="comment-thread">
      <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-[0.12em] text-muted">
        <MessageSquare className="h-3.5 w-3.5" aria-hidden="true" />
        Comments
      </div>

      {loading ? (
        <p className="text-sm text-muted">Loading comments…</p>
      ) : comments.length === 0 ? (
        <p className="text-sm text-muted">No comments yet.</p>
      ) : (
        <ul className="mb-3 flex flex-col gap-2">
          {comments.map((comment) => (
            <li
              key={comment.id}
              className="flex items-start justify-between gap-2 rounded border border-line bg-panel px-3 py-2 text-sm"
            >
              <div className="min-w-0">
                <p className="whitespace-pre-wrap text-body">{comment.body}</p>
                <p className="mt-1 text-xs text-muted">
                  {new Date(comment.created_at).toLocaleString()}
                  {comment.resolved_at && " · Resolved"}
                </p>
              </div>
              {!comment.resolved_at && (
                <button
                  type="button"
                  onClick={() => void handleResolve(comment.id)}
                  className="inline-flex shrink-0 items-center gap-1 rounded p-1 text-xs font-medium text-muted transition hover:bg-white hover:text-ink"
                  aria-label="Resolve comment"
                  title="Resolve"
                >
                  <Check className="h-3.5 w-3.5" aria-hidden="true" />
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {error && <p className="mb-2 text-xs text-danger">{error}</p>}

      <div className="flex items-start gap-2">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Add a comment…"
          rows={2}
          className="min-w-0 flex-1 rounded border border-line-strong bg-white px-2 py-1.5 text-sm text-ink outline-none focus:border-ink"
        />
        <button
          type="button"
          onClick={() => void handleSubmit()}
          disabled={!draft.trim() || submitting}
          className="inline-flex h-8 shrink-0 items-center rounded-md bg-primary px-3 text-sm font-medium text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45"
        >
          Post
        </button>
      </div>
    </div>
  );
}
