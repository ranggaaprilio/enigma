import {
  type ChangeEvent,
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ChevronDown,
  ChevronRight,
  Download,
  FileText,
  Folder,
  FolderPlus,
  RefreshCw,
  Search,
  Trash2,
  Upload,
  X,
} from "lucide-react";

import {
  ApiError,
  createWorkspaceFolder,
  deleteWorkspaceFile,
  getWorkspaceFileDownloadUrl,
  getWorkspaceFileTree,
  renameWorkspaceFile,
  searchWorkspaceFiles,
  uploadWorkspaceFile,
  type WorkspaceFileNode,
  type WorkspaceResponse,
} from "../lib/api";
import { useActiveWorktree } from "../lib/activeWorktree";

type PendingAction =
  | { type: "rename"; node: WorkspaceFileNode; newPath: string }
  | { type: "delete"; node: WorkspaceFileNode };

export function FileManagementPanel({ workspace }: { workspace: WorkspaceResponse }) {
  // The Files view follows the worktree selected in the Worktrees panel (broadcast via
  // useActiveWorktree), so browsing and every file operation target the active branch's
  // working tree. null => the base clone.
  const [active] = useActiveWorktree(workspace.id);
  const worktreeId = active?.id;
  const [tree, setTree] = useState<WorkspaceFileNode | null>(null);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(() => new Set([""]));
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<WorkspaceFileNode[]>([]);
  const [folderPath, setFolderPath] = useState("");
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const uploadInputRef = useRef<HTMLInputElement | null>(null);

  const selectedNode = useMemo(
    () => (selectedPath && tree ? findNode(tree, selectedPath) : tree),
    [selectedPath, tree],
  );

  const loadTree = useCallback(async (signal?: AbortSignal) => {
    setError(null);
    setBusyAction("tree");
    try {
      const nextTree = await getWorkspaceFileTree(workspace.id, "", worktreeId, signal);
      setTree(nextTree);
      setSelectedPath((current) => current ?? nextTree.path);
    } catch (caughtError) {
      if (caughtError instanceof DOMException && caughtError.name === "AbortError") {
        return;
      }
      setError(caughtError instanceof ApiError ? caughtError.message : "Unable to load files.");
    } finally {
      setBusyAction((current) => (current === "tree" ? null : current));
    }
  }, [workspace.id, worktreeId]);

  useEffect(() => {
    const controller = new AbortController();
    setTree(null);
    setSelectedPath(null);
    setExpandedPaths(new Set([""]));
    setSearchQuery("");
    setSearchResults([]);
    void loadTree(controller.signal);
    return () => controller.abort();
  }, [loadTree, workspace.id]);

  useEffect(() => {
    const query = searchQuery.trim();
    if (!query) {
      setSearchResults([]);
      return;
    }

    const controller = new AbortController();
    searchWorkspaceFiles(workspace.id, query, worktreeId, controller.signal)
      .then((results) => setSearchResults(results))
      .catch((caughtError: unknown) => {
        if (caughtError instanceof DOMException && caughtError.name === "AbortError") {
          return;
        }
        setError(caughtError instanceof ApiError ? caughtError.message : "Unable to search files.");
      });

    return () => controller.abort();
  }, [searchQuery, workspace.id, worktreeId]);

  function toggleFolder(path: string) {
    setExpandedPaths((current) => {
      const next = new Set(current);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  }

  async function createFolder(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!folderPath.trim()) {
      setError("Enter a folder path.");
      return;
    }
    await runMutation("folder", async () => {
      await createWorkspaceFolder(workspace.id, { path: folderPath }, worktreeId);
      setExpandedPaths((current) => new Set(current).add(parentPath(folderPath)));
      setFolderPath("");
      return "Folder created.";
    });
  }

  async function uploadFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) {
      return;
    }
    const destination =
      selectedNode?.type === "directory" ? selectedNode.path : parentPath(selectedNode?.path ?? "");
    await runMutation("upload", async () => {
      await uploadWorkspaceFile(workspace.id, destination, file, file.name, worktreeId);
      return "File uploaded.";
    });
    event.target.value = "";
  }

  async function confirmAction() {
    if (!pendingAction) {
      return;
    }
    if (pendingAction.type === "delete") {
      await runMutation("delete", async () => {
        await deleteWorkspaceFile(workspace.id, pendingAction.node.path, worktreeId);
        setSelectedPath(null);
        setPendingAction(null);
        return "File deleted.";
      });
      return;
    }
    await runMutation("rename", async () => {
      const response = await renameWorkspaceFile(
        workspace.id,
        {
          path: pendingAction.node.path,
          new_path: pendingAction.newPath,
        },
        worktreeId,
      );
      setSelectedPath(response.path);
      setPendingAction(null);
      return "File renamed.";
    });
  }

  async function runMutation(action: string, mutation: () => Promise<string>) {
    setBusyAction(action);
    setError(null);
    setMessage(null);
    try {
      const nextMessage = await mutation();
      setMessage(nextMessage);
      await loadTree();
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : "File operation failed.");
    } finally {
      setBusyAction(null);
    }
  }

  function downloadSelected() {
    if (!selectedNode || selectedNode.type !== "file") {
      return;
    }
    window.open(
      getWorkspaceFileDownloadUrl(workspace.id, selectedNode.path, worktreeId),
      "_blank",
      "noopener",
    );
  }

  const visibleNodes = searchQuery.trim() ? searchResults : tree?.children ?? [];

  return (
    <section className="grid gap-4 xl:grid-cols-[360px_minmax(0,1fr)]">
      <div className="rounded-md border border-line bg-white p-5 shadow-soft">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-sm font-medium uppercase tracking-[0.16em] text-ink/55">
              Files
            </p>
            <h3 className="mt-1 text-base font-semibold">{workspace.name}</h3>
            <p className="mt-1 truncate font-mono text-xs text-ink/55" title="Active branch">
              {active?.branch ?? workspace.default_branch}
            </p>
          </div>
          <button
            aria-label="Refresh files"
            className="flex h-9 w-9 items-center justify-center rounded-md border border-line bg-white text-ink/70 transition hover:bg-panel hover:text-ink"
            type="button"
            disabled={busyAction === "tree"}
            onClick={() => void loadTree()}
          >
            <RefreshCw className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>

        <label className="mt-4 flex items-center gap-2 rounded-md border border-line bg-white px-3 py-2 text-sm">
          <Search className="h-4 w-4 text-ink/45" aria-hidden="true" />
          <input
            className="min-w-0 flex-1 bg-transparent outline-none"
            placeholder="Search files"
            type="search"
            value={searchQuery}
            onChange={(event) => setSearchQuery(event.target.value)}
          />
        </label>

        <div className="mt-3 flex flex-wrap gap-2">
          <button
            className="inline-flex items-center gap-2 rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel"
            type="button"
            onClick={() => uploadInputRef.current?.click()}
          >
            <Upload className="h-4 w-4" aria-hidden="true" />
            Upload
          </button>
          <input ref={uploadInputRef} className="hidden" type="file" onChange={uploadFile} />
        </div>

        <form className="mt-3 flex gap-2" onSubmit={createFolder}>
          <input
            className="min-w-0 flex-1 rounded-md border border-line bg-white px-3 py-2 text-sm outline-none transition focus:border-ink"
            placeholder="docs/guides"
            value={folderPath}
            onChange={(event) => setFolderPath(event.target.value)}
          />
          <button
            aria-label="Create folder"
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-primary text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45"
            type="submit"
            disabled={busyAction === "folder"}
          >
            <FolderPlus className="h-4 w-4" aria-hidden="true" />
          </button>
        </form>

        {error && (
          <div className="mt-4 rounded-md border border-danger/30 bg-[#fff5f2] p-3 text-sm text-danger">
            {error}
          </div>
        )}
        {message && (
          <div className="mt-4 rounded-md border border-signal/30 bg-[#f0faf5] p-3 text-sm text-signal">
            {message}
          </div>
        )}

        <div className="mt-4 max-h-[620px] overflow-auto rounded-md border border-line bg-white p-2">
          {busyAction === "tree" && !tree && (
            <div className="p-4 text-sm text-ink/65">Loading files...</div>
          )}
          {visibleNodes.length === 0 && tree && (
            <div className="p-4 text-sm text-ink/65">
              {searchQuery.trim() ? "No matching files." : "No files yet."}
            </div>
          )}
          {searchQuery.trim()
            ? visibleNodes.map((node) => (
                <FileRow
                  key={node.path}
                  node={node}
                  depth={0}
                  expanded={false}
                  selected={selectedNode?.path === node.path}
                  onSelect={() => setSelectedPath(node.path)}
                  onToggle={() => undefined}
                />
              ))
            : tree?.children.map((node) => (
                <TreeNode
                  key={node.path}
                  node={node}
                  depth={0}
                  expandedPaths={expandedPaths}
                  selectedPath={selectedNode?.path ?? ""}
                  onSelect={(nodePath) => setSelectedPath(nodePath)}
                  onToggle={toggleFolder}
                />
              ))}
        </div>
      </div>

      <div className="rounded-md border border-line bg-white p-5 shadow-soft">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-sm font-medium uppercase tracking-[0.16em] text-ink/55">
              Selection
            </p>
            <h3 className="mt-1 truncate text-base font-semibold">
              {selectedNode?.path || "Workspace root"}
            </h3>
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              className="inline-flex items-center gap-2 rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
              type="button"
              disabled={!selectedNode || selectedNode.type !== "file"}
              onClick={downloadSelected}
            >
              <Download className="h-4 w-4" aria-hidden="true" />
              Download
            </button>
            <button
              className="inline-flex items-center gap-2 rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
              type="button"
              disabled={!selectedNode || selectedNode.path === ""}
              onClick={() =>
                selectedNode &&
                setPendingAction({ type: "rename", node: selectedNode, newPath: selectedNode.path })
              }
            >
              Rename
            </button>
            <button
              className="inline-flex items-center gap-2 rounded-md border border-danger/30 bg-white px-3 py-2 text-sm font-medium text-danger transition hover:bg-[#fff5f2] disabled:cursor-not-allowed disabled:opacity-50"
              type="button"
              disabled={!selectedNode || selectedNode.path === ""}
              onClick={() => selectedNode && setPendingAction({ type: "delete", node: selectedNode })}
            >
              <Trash2 className="h-4 w-4" aria-hidden="true" />
              Delete
            </button>
          </div>
        </div>

        {selectedNode ? (
          <dl className="mt-5 grid gap-3 sm:grid-cols-2">
            <FileMetric label="Name" value={selectedNode.name} />
            <FileMetric label="Type" value={selectedNode.type} />
            <FileMetric label="Size" value={formatSize(selectedNode.size)} />
            <FileMetric label="Modified" value={new Date(selectedNode.modified_at).toLocaleString()} />
          </dl>
        ) : (
          <div className="mt-5 rounded-md border border-dashed border-line bg-white p-6 text-ink/72">
            Select a file or folder.
          </div>
        )}

        <button
          className="mt-5 rounded-md border border-line bg-white px-3 py-2 text-sm font-medium text-ink/50"
          type="button"
          disabled
        >
          Open in editor
        </button>
      </div>

      {pendingAction && (
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-ink/45 p-4">
          <div className="w-full max-w-md rounded-md border border-line bg-white p-5 shadow-soft">
            <div className="flex items-start justify-between gap-3">
              <h3 className="text-base font-semibold">
                {pendingAction.type === "delete" ? "Confirm delete" : "Confirm rename"}
              </h3>
              <button
                aria-label="Cancel file action"
                className="flex h-8 w-8 items-center justify-center rounded-md border border-line bg-white"
                type="button"
                onClick={() => setPendingAction(null)}
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
            {pendingAction.type === "delete" ? (
              <p className="mt-4 text-sm text-ink/70">
                Delete <span className="font-mono font-semibold">{pendingAction.node.path}</span>?
              </p>
            ) : (
              <label className="mt-4 block text-sm font-medium">
                New path
                <input
                  className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 font-mono text-sm outline-none transition focus:border-ink"
                  value={pendingAction.newPath}
                  onChange={(event) =>
                    setPendingAction({ ...pendingAction, newPath: event.target.value })
                  }
                />
              </label>
            )}
            <div className="mt-5 flex justify-end gap-2">
              <button
                className="rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel"
                type="button"
                onClick={() => setPendingAction(null)}
              >
                Cancel
              </button>
              <button
                className="rounded-md bg-primary px-3 py-2 text-sm font-semibold text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45"
                type="button"
                disabled={
                  busyAction === pendingAction.type ||
                  (pendingAction.type === "rename" && !pendingAction.newPath.trim())
                }
                onClick={() => void confirmAction()}
              >
                {pendingAction.type === "delete" ? "Delete" : "Rename"}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

function TreeNode({
  node,
  depth,
  expandedPaths,
  selectedPath,
  onSelect,
  onToggle,
}: {
  node: WorkspaceFileNode;
  depth: number;
  expandedPaths: Set<string>;
  selectedPath: string;
  onSelect: (path: string) => void;
  onToggle: (path: string) => void;
}) {
  const expanded = expandedPaths.has(node.path);
  return (
    <div>
      <FileRow
        node={node}
        depth={depth}
        expanded={expanded}
        selected={selectedPath === node.path}
        onSelect={() => onSelect(node.path)}
        onToggle={() => onToggle(node.path)}
      />
      {node.type === "directory" &&
        expanded &&
        node.children.map((child) => (
          <TreeNode
            key={child.path}
            node={child}
            depth={depth + 1}
            expandedPaths={expandedPaths}
            selectedPath={selectedPath}
            onSelect={onSelect}
            onToggle={onToggle}
          />
        ))}
    </div>
  );
}

function FileRow({
  node,
  depth,
  expanded,
  selected,
  onSelect,
  onToggle,
}: {
  node: WorkspaceFileNode;
  depth: number;
  expanded: boolean;
  selected: boolean;
  onSelect: () => void;
  onToggle: () => void;
}) {
  const Icon = node.type === "directory" ? Folder : FileText;
  return (
    <div
      className={`flex items-center gap-1 rounded-md px-2 py-1.5 text-left text-sm transition ${
        selected ? "bg-line text-ink" : "text-ink/72 hover:bg-panel hover:text-ink"
      }`}
      style={{ paddingLeft: `${depth * 16 + 8}px` }}
    >
      <button
        aria-label={`${expanded ? "Collapse" : "Expand"} ${node.name}`}
        className="flex h-5 w-5 shrink-0 items-center justify-center rounded text-ink/55 disabled:opacity-0"
        type="button"
        disabled={node.type !== "directory"}
        onClick={onToggle}
      >
        {expanded ? (
          <ChevronDown className="h-4 w-4" aria-hidden="true" />
        ) : (
          <ChevronRight className="h-4 w-4" aria-hidden="true" />
        )}
      </button>
      <button
        className="flex min-w-0 flex-1 items-center gap-2 text-left"
        type="button"
        onClick={onSelect}
      >
        <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
        <span className="truncate font-mono">{node.path || node.name}</span>
      </button>
    </div>
  );
}

function FileMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-line bg-white p-4">
      <dt className="text-sm text-ink/60">{label}</dt>
      <dd className="mt-2 break-words font-mono text-sm">{value}</dd>
    </div>
  );
}

function findNode(node: WorkspaceFileNode, path: string): WorkspaceFileNode | null {
  if (node.path === path) {
    return node;
  }
  for (const child of node.children) {
    const match = findNode(child, path);
    if (match) {
      return match;
    }
  }
  return null;
}

function parentPath(path: string) {
  const parts = path.split("/").filter(Boolean);
  parts.pop();
  return parts.join("/");
}

function formatSize(size: number | null) {
  if (size === null) {
    return "-";
  }
  if (size < 1024) {
    return `${size} B`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}
