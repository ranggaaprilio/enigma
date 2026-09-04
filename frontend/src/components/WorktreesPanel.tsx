import { type FormEvent, useCallback, useEffect, useState } from "react";

import {
  ApiError,
  commitWorktree,
  createPullRequest,
  createWorktree,
  listPullRequests,
  listWorktrees,
  pullWorktree,
  pushWorktree,
  removeWorktree,
  syncWorktree,
  type PullRequestResponse,
  type RepositoryCredentialResponse,
  type WorkspaceResponse,
  type WorktreeResponse,
} from "../lib/api";
import {
  type ActiveWorktree,
  reconcileActiveWorktree,
  useActiveWorktree,
} from "../lib/activeWorktree";
import { detectProvider, prNoun, providerHost, providerLabel } from "../lib/gitProviders";
import { CommentThread } from "./comments/CommentThread";

type LoadState =
  | { status: "loading" }
  | { status: "ready" }
  | { status: "error"; message: string };

export function WorktreesPanel({
  workspace,
  credential,
}: {
  workspace: WorkspaceResponse;
  /** The workspace's git credential; `undefined`/`null` = unknown, keep the GitHub defaults. */
  credential?: RepositoryCredentialResponse | null;
}) {
  const provider = credential?.provider ?? detectProvider(workspace.repo_url ?? "");
  const noun = prNoun(provider);
  const nounTitle = noun.charAt(0).toUpperCase() + noun.slice(1);
  // Provider-native pull requests are only available for GitHub + token credentials.
  const prSupported = credential?.supports_pull_requests !== false;
  const [worktrees, setWorktrees] = useState<WorktreeResponse[]>([]);
  const [pullRequests, setPullRequests] = useState<PullRequestResponse[]>([]);
  const [expandedPrIds, setExpandedPrIds] = useState<Set<string>>(new Set());
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [active, setActive] = useActiveWorktree(workspace.id);

  // New-worktree form.
  const [newBranch, setNewBranch] = useState("");
  const [newBase, setNewBase] = useState(workspace.default_branch);

  // Open-PR form.
  const [prWorktreeId, setPrWorktreeId] = useState<string>("");
  const [prTitle, setPrTitle] = useState("");
  const [prBody, setPrBody] = useState("");
  const [prBase, setPrBase] = useState(workspace.default_branch);
  const [prDraft, setPrDraft] = useState(false);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const [trees, prs] = await Promise.all([
          listWorktrees(workspace.id, signal),
          listPullRequests(workspace.id, signal).catch(() => [] as PullRequestResponse[]),
        ]);
        setWorktrees(trees);
        setPullRequests(prs);
        // This is the authoritative list, so use it to drop a selection (in localStorage,
        // possibly written by another tab) that points at a worktree that is now gone.
        reconcileActiveWorktree(workspace.id, trees);
        setState({ status: "ready" });
      } catch (caughtError) {
        if (caughtError instanceof DOMException && caughtError.name === "AbortError") return;
        setState({
          status: "error",
          message: caughtError instanceof ApiError ? caughtError.message : "Unable to load worktrees.",
        });
      }
    },
    [workspace.id],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const featureWorktrees = worktrees.filter((tree) => !tree.is_base);

  // Default the PR source to the active (or first feature) worktree once data arrives, and
  // re-default it whenever the chosen source disappears — otherwise a removed worktree
  // stayed selected and "Open PR" targeted a branch that no longer has a checkout.
  useEffect(() => {
    if (prWorktreeId && featureWorktrees.some((tree) => tree.id === prWorktreeId)) return;
    const fallback = active?.id ?? featureWorktrees[0]?.id ?? "";
    if (fallback !== prWorktreeId) setPrWorktreeId(fallback);
  }, [active?.id, featureWorktrees, prWorktreeId]);

  async function runAction(name: string, action: () => Promise<string>) {
    setBusy(name);
    setError(null);
    setMessage(null);
    try {
      const result = await action();
      setMessage(result);
      await load();
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : "Action failed.");
    } finally {
      setBusy(null);
    }
  }

  function selectWorktree(tree: WorktreeResponse) {
    const next: ActiveWorktree | null = tree.is_base ? null : { id: tree.id, branch: tree.branch };
    setActive(next);
  }

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!newBranch.trim()) {
      setError("Enter a branch name.");
      return;
    }
    await runAction("create", async () => {
      const created = await createWorktree(workspace.id, {
        branch: newBranch.trim(),
        base: newBase.trim() || undefined,
      });
      setActive({ id: created.id, branch: created.branch });
      setNewBranch("");
      return `Worktree for '${created.branch}' created.`;
    });
  }

  async function handleRemove(tree: WorktreeResponse) {
    const force = !tree.is_clean;
    if (force && !window.confirm(`'${tree.branch}' has uncommitted changes. Remove anyway?`)) {
      return;
    }
    await runAction(`remove-${tree.id}`, async () => {
      await removeWorktree(workspace.id, tree.id, force);
      if (active?.id === tree.id) setActive(null);
      return `Worktree '${tree.branch}' removed.`;
    });
  }

  async function handlePush(tree: WorktreeResponse) {
    await runAction(`push-${tree.id}`, async () => {
      const response = await pushWorktree(workspace.id, tree.id);
      return response.message;
    });
  }

  async function handlePull(tree: WorktreeResponse) {
    await runAction(`pull-${tree.id}`, async () => {
      const response = await pullWorktree(workspace.id, tree.id);
      return response.message;
    });
  }

  async function handleSync(tree: WorktreeResponse) {
    await runAction(`sync-${tree.id}`, async () => {
      const response = await syncWorktree(workspace.id, tree.id);
      return response.message;
    });
  }

  async function handleCommitActive(tree: WorktreeResponse) {
    const messageText = window.prompt(`Commit message for '${tree.branch}':`);
    if (!messageText || !messageText.trim()) return;
    await runAction(`commit-${tree.id}`, async () => {
      const response = await commitWorktree(workspace.id, tree.id, { message: messageText.trim() });
      return response.message;
    });
  }

  async function handleOpenPr(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!prWorktreeId) {
      setError("Choose a source worktree.");
      return;
    }
    if (!prTitle.trim()) {
      setError(`Enter a ${noun} title.`);
      return;
    }
    await runAction("open-pr", async () => {
      const pr = await createPullRequest(workspace.id, {
        worktree_id: prWorktreeId,
        base: prBase.trim() || undefined,
        title: prTitle.trim(),
        body: prBody.trim() || undefined,
        draft: prDraft,
      });
      setPrTitle("");
      setPrBody("");
      return `${nounTitle} #${pr.number} opened.`;
    });
  }

  return (
    <section className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
      <div className="rounded-md border border-line bg-white p-5 shadow-soft">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="text-sm font-medium uppercase tracking-[0.16em] text-ink/55">
              Worktrees
            </p>
            <h3 className="mt-1 text-base font-semibold">Branches</h3>
          </div>
          <button
            className="rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel"
            type="button"
            onClick={() => void load()}
          >
            Refresh
          </button>
        </div>

        {state.status === "loading" && (
          <p className="mt-4 text-sm text-ink/60">Loading worktrees…</p>
        )}
        {state.status === "error" && (
          <div className="mt-4 rounded-md border border-danger/30 bg-[#fff5f2] px-3 py-2 text-sm text-danger">
            {state.message}
          </div>
        )}

        {state.status === "ready" && (
          <ul className="mt-4 space-y-2">
            {worktrees.map((tree) => {
              const isActive = tree.is_base ? active === null : active?.id === tree.id;
              return (
                <li
                  key={tree.id}
                  className={`flex flex-wrap items-center justify-between gap-3 rounded-md border px-3 py-2 transition ${
                    isActive ? "border-ink bg-white" : "border-line bg-white hover:bg-panel"
                  }`}
                >
                  <button
                    type="button"
                    className="flex min-w-0 flex-1 items-center gap-2 text-left"
                    onClick={() => selectWorktree(tree)}
                  >
                    <span
                      className={`h-2.5 w-2.5 shrink-0 rounded-full ${
                        isActive ? "bg-primary" : "bg-line"
                      }`}
                      aria-hidden="true"
                    />
                    <span className="min-w-0">
                      <span className="block truncate font-mono text-sm font-medium text-ink">
                        {tree.branch}
                        {tree.is_base && (
                          <span className="ml-2 rounded bg-line px-1.5 py-0.5 text-xs font-normal text-ink/60">
                            base
                          </span>
                        )}
                      </span>
                      <span className="block truncate text-xs text-ink/55">
                        {tree.is_clean ? "clean" : "uncommitted changes"}
                        {!tree.is_base && ` · ahead ${tree.ahead} · behind ${tree.behind}`}
                      </span>
                    </span>
                  </button>
                  <div className="flex shrink-0 items-center gap-1.5">
                    {!tree.is_base && (
                      <>
                        <SmallButton
                          label="Commit"
                          disabled={busy !== null || tree.is_clean}
                          onClick={() => void handleCommitActive(tree)}
                        />
                        <SmallButton
                          label={busy === `pull-${tree.id}` ? "Pulling…" : "Pull"}
                          disabled={busy !== null}
                          onClick={() => void handlePull(tree)}
                        />
                        <SmallButton
                          label={busy === `sync-${tree.id}` ? "Updating…" : "Update"}
                          title={`Merge ${tree.base_ref} into ${tree.branch}`}
                          disabled={busy !== null}
                          onClick={() => void handleSync(tree)}
                        />
                        <SmallButton
                          label={busy === `push-${tree.id}` ? "Pushing…" : "Push"}
                          disabled={busy !== null}
                          onClick={() => void handlePush(tree)}
                        />
                        <SmallButton
                          label="Remove"
                          disabled={busy !== null}
                          onClick={() => void handleRemove(tree)}
                        />
                      </>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}

        <form className="mt-5 grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]" onSubmit={handleCreate}>
          <input
            className="rounded-md border border-line bg-white px-3 py-2 text-sm outline-none focus:border-ink"
            placeholder="New branch (e.g. feat/login)"
            value={newBranch}
            onChange={(event) => setNewBranch(event.target.value)}
          />
          <input
            className="rounded-md border border-line bg-white px-3 py-2 text-sm outline-none focus:border-ink"
            placeholder="Base ref"
            value={newBase}
            onChange={(event) => setNewBase(event.target.value)}
          />
          <button
            className="rounded-md bg-primary px-4 py-2 text-sm font-semibold text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45"
            type="submit"
            disabled={busy === "create"}
          >
            {busy === "create" ? "Creating…" : "New worktree"}
          </button>
        </form>

        {message && (
          <div className="mt-4 rounded-md border border-signal/30 bg-[#f0faf5] px-3 py-2 text-sm text-signal">
            {message}
          </div>
        )}
        {error && (
          <div className="mt-4 rounded-md border border-danger/30 bg-[#fff5f2] px-3 py-2 text-sm text-danger">
            {error}
          </div>
        )}
      </div>

      <div className="space-y-4">
        {!prSupported ? (
          <div className="rounded-md border border-dashed border-line bg-canvas p-4 text-sm text-body">
            <h3 className="text-base font-semibold text-ink">{nounTitle}s</h3>
            <p className="mt-2">
              {nounTitle}s for {providerLabel(provider)} are not supported yet. Push the branch and
              open it on {providerHost(provider, workspace.repo_url)}.
            </p>
          </div>
        ) : (
        <div className="rounded-md border border-line bg-white p-5 shadow-soft">
          <h3 className="text-base font-semibold">Open {noun}</h3>
          <form className="mt-3 space-y-3" onSubmit={handleOpenPr}>
            <label className="block text-sm">
              <span className="mb-1 block text-ink/65">Source worktree</span>
              <select
                className="w-full rounded-md border border-line bg-white px-3 py-2 text-sm outline-none focus:border-ink"
                value={prWorktreeId}
                onChange={(event) => setPrWorktreeId(event.target.value)}
              >
                <option value="" disabled>
                  Select a branch
                </option>
                {featureWorktrees.map((tree) => (
                  <option key={tree.id} value={tree.id}>
                    {tree.branch}
                  </option>
                ))}
              </select>
            </label>
            <label className="block text-sm">
              <span className="mb-1 block text-ink/65">Title</span>
              <input
                className="w-full rounded-md border border-line bg-white px-3 py-2 text-sm outline-none focus:border-ink"
                value={prTitle}
                onChange={(event) => setPrTitle(event.target.value)}
              />
            </label>
            <label className="block text-sm">
              <span className="mb-1 block text-ink/65">Description</span>
              <textarea
                className="min-h-20 w-full resize-y rounded-md border border-line bg-white px-3 py-2 text-sm outline-none focus:border-ink"
                value={prBody}
                onChange={(event) => setPrBody(event.target.value)}
              />
            </label>
            <label className="block text-sm">
              <span className="mb-1 block text-ink/65">Base branch</span>
              <input
                className="w-full rounded-md border border-line bg-white px-3 py-2 text-sm outline-none focus:border-ink"
                value={prBase}
                onChange={(event) => setPrBase(event.target.value)}
              />
            </label>
            <label className="flex items-center gap-2 text-sm text-ink/70">
              <input
                type="checkbox"
                checked={prDraft}
                onChange={(event) => setPrDraft(event.target.checked)}
              />
              Draft {noun}
            </label>
            <button
              className="w-full rounded-md bg-primary px-4 py-2 text-sm font-semibold text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45"
              type="submit"
              disabled={busy === "open-pr" || featureWorktrees.length === 0}
            >
              {busy === "open-pr" ? "Opening…" : `Open ${noun}`}
            </button>
          </form>
        </div>
        )}

        <div className="rounded-md border border-line bg-white p-5 shadow-soft">
          <h3 className="text-base font-semibold">{nounTitle}s</h3>
          {pullRequests.length === 0 ? (
            <p className="mt-3 text-sm text-ink/60">No {noun}s opened from this workspace yet.</p>
          ) : (
            <ul className="mt-3 space-y-2">
              {pullRequests.map((pr) => (
                <li
                  key={pr.id}
                  className="rounded-md border border-line px-3 py-2 text-sm"
                >
                  <div className="flex items-center justify-between gap-2">
                    <a
                      className="truncate font-medium text-primary hover:underline"
                      href={pr.url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      #{pr.number} {pr.title}
                    </a>
                    <span className="shrink-0 rounded-full border border-line px-2 py-0.5 text-xs capitalize text-ink/65">
                      {pr.state}
                    </span>
                  </div>
                  <p className="mt-1 font-mono text-xs text-ink/55">
                    {pr.head} → {pr.base}
                  </p>
                  <button
                    type="button"
                    onClick={() =>
                      setExpandedPrIds((prev) => {
                        const next = new Set(prev);
                        if (next.has(pr.id)) next.delete(pr.id);
                        else next.add(pr.id);
                        return next;
                      })
                    }
                    className="mt-2 text-xs font-medium text-primary hover:underline"
                  >
                    {expandedPrIds.has(pr.id) ? "Hide comments" : "Show comments"}
                  </button>
                  {expandedPrIds.has(pr.id) && (
                    <div className="mt-2">
                      <CommentThread targetType="pr" targetId={pr.id} />
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </section>
  );
}

function SmallButton({
  label,
  disabled,
  onClick,
  title,
}: {
  label: string;
  disabled: boolean;
  onClick: () => void;
  title?: string;
}) {
  return (
    <button
      type="button"
      title={title}
      className="rounded-md border border-line bg-white px-2.5 py-1 text-xs font-medium transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
      disabled={disabled}
      onClick={onClick}
    >
      {label}
    </button>
  );
}
