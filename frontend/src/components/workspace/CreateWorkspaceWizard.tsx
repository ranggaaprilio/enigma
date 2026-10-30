import { useEffect, useState } from "react";
import { Box, Check, FolderGit2, LayoutTemplate, X } from "lucide-react";

import {
  ApiError,
  cloneRepository,
  connectRepository,
  getHealth,
  getRepositoryCredential,
  listWorkspaceTemplates,
  setupDevServer,
  type GitProviderChoice,
  type HealthResponse,
  type RepositoryAuthType,
  type RepositoryCredentialResponse,
  type RuntimeName,
  type WorkspaceCreatePayload,
  type WorkspaceResponse,
  type WorkspaceSource,
  type WorkspaceTemplateResponse,
} from "../../lib/api";
import {
  PROVIDER_HELP,
  authTypeLabel,
  detectProvider,
  parseGitRemote,
  providerLabel,
  resolveProvider,
} from "../../lib/gitProviders";
import { defaultRuntime, runtimeLabel, runtimeOptions } from "../../lib/runtime";
import { TextInput } from "../ui/TextInput";
import { ProviderAuthSelector } from "./ProviderAuthSelector";
import { TemplateGallery } from "./TemplateGallery";

type Step = "source" | "details" | "access" | "review";

const STEP_LABELS: Record<Step, string> = {
  source: "Source",
  details: "Details",
  access: "Access",
  review: "Review",
};

type WizardForm = {
  step: Step;
  source: WorkspaceSource | null;
  name: string;
  defaultBranch: string;
  snapshot: string;
  /** Null until `/health` says what is available; null with no options = the server picks. */
  runtime: RuntimeName | null;
  template: WorkspaceTemplateResponse | null;
  repoUrl: string;
  provider: GitProviderChoice;
  useCredentials: boolean;
  authType: RepositoryAuthType;
  token: string;
  username: string;
  cloneAfterCreate: boolean;
};

function initialForm(): WizardForm {
  return {
    step: "source",
    source: null,
    name: "",
    defaultBranch: "main",
    snapshot: "",
    runtime: null,
    template: null,
    repoUrl: "",
    provider: "auto",
    useCredentials: true,
    authType: "personal_access_token",
    token: "",
    username: "",
    cloneAfterCreate: true,
  };
}

type Outcome = {
  workspace: WorkspaceResponse;
  credential: RepositoryCredentialResponse | null;
  postCreateError: string | null;
};

const SOURCE_OPTIONS: {
  value: WorkspaceSource;
  title: string;
  body: string;
  Icon: typeof Box;
}[] = [
  {
    value: "template",
    title: "Start from a template",
    body: "Pick a starter and get a ready-to-run repository.",
    Icon: LayoutTemplate,
  },
  {
    value: "blank",
    title: "Blank workspace",
    body: "An empty git repository in a fresh sandbox.",
    Icon: Box,
  },
  {
    value: "repository",
    title: "Connect an existing repository",
    body: "Clone from GitHub, GitLab, Bitbucket, or any git host.",
    Icon: FolderGit2,
  },
];

export function CreateWorkspaceWizard({
  open,
  onClose,
  onCreated,
  createFn,
  subtitle = "Provisions a sandbox for the workspace.",
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (workspace: WorkspaceResponse) => void;
  createFn: (payload: WorkspaceCreatePayload) => Promise<WorkspaceResponse>;
  subtitle?: string;
}) {
  const [form, setForm] = useState<WizardForm>(initialForm);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [templates, setTemplates] = useState<WorkspaceTemplateResponse[] | null>(null);
  const [templatesLoading, setTemplatesLoading] = useState(false);
  const [templatesError, setTemplatesError] = useState<string | null>(null);
  const [templatesAttempt, setTemplatesAttempt] = useState(0);
  const [copyLabel, setCopyLabel] = useState("Copy");
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthAttempt, setHealthAttempt] = useState(0);

  const patch = (values: Partial<WizardForm>) => setForm((prev) => ({ ...prev, ...values }));

  useEffect(() => {
    if (!open) return;
    setForm(initialForm());
    setError(null);
    setSubmitting(false);
    setOutcome(null);
  }, [open]);

  // Which runtimes can take a workspace (local-docker-runtime Epic 06). Unknown health leaves
  // the choice to the server, which validates it anyway.
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setHealth(null);
    getHealth(controller.signal)
      .then((data) => {
        setHealth(data);
        setForm((prev) => (prev.runtime ? prev : { ...prev, runtime: defaultRuntime(data) }));
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [open, healthAttempt]);

  useEffect(() => {
    if (!open || form.source !== "template") return;
    const controller = new AbortController();
    setTemplatesLoading(true);
    setTemplatesError(null);
    listWorkspaceTemplates(controller.signal)
      .then((data) => {
        setTemplates(Array.isArray(data) ? data : []);
        setTemplatesLoading(false);
      })
      .catch((caughtError: unknown) => {
        if (caughtError instanceof DOMException && caughtError.name === "AbortError") return;
        setTemplatesError(
          caughtError instanceof ApiError ? caughtError.message : "Unable to load templates.",
        );
        setTemplatesLoading(false);
      });
    return () => controller.abort();
  }, [open, form.source, templatesAttempt]);

  const created = outcome?.workspace ?? null;

  function handleClose() {
    if (submitting) return;
    if (created) onCreated(created);
    else onClose();
  }

  useEffect(() => {
    if (!open) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") handleClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
    // handleClose reads the latest state via closure; re-binding on each render is intended.
  });

  if (!open) return null;

  const steps: Step[] =
    form.source === "repository"
      ? ["source", "details", "access", "review"]
      : ["source", "details", "review"];
  const stepIndex = steps.indexOf(form.step);
  const resolvedProvider = resolveProvider(form.repoUrl, form.provider);
  const help = PROVIDER_HELP[resolvedProvider];
  const remote = parseGitRemote(form.repoUrl);
  const runtimes = runtimeOptions(health);
  // A snapshot names a Daytona image; Docker always runs DOCKER_SANDBOX_IMAGE.
  const usesSnapshot = form.runtime !== "docker";

  function validate(step: Step): string | null {
    if (step === "source") return form.source ? null : "Choose how to start the workspace.";
    if (step === "details") {
      if (!form.name.trim()) return "Enter a workspace name.";
      if (form.source === "template" && !form.template) return "Pick a template.";
      if (form.source !== "template" && !form.defaultBranch.trim()) {
        return "Enter a default branch.";
      }
      if (form.source === "repository" && !remote) {
        return "Enter an HTTPS or SSH repository URL.";
      }
      if (runtimes.length > 0 && !form.runtime) {
        return "No sandbox runtime is available. Ask an administrator to configure Daytona or Docker.";
      }
      return null;
    }
    if (step === "access") {
      if (!form.useCredentials) return null;
      if (form.authType === "personal_access_token") {
        if (remote?.scheme !== "https") return "Access tokens need an HTTPS repository URL.";
        if (!form.token.trim()) return `Enter your ${help.tokenLabel.toLowerCase()}.`;
        if (help.requiresUsername && !form.username.trim()) return "Enter your username.";
      } else if (remote?.scheme !== "ssh") {
        return "SSH deploy keys need an SSH URL such as git@host:owner/repo.git.";
      }
      return null;
    }
    return null;
  }

  function goNext() {
    const problem = validate(form.step);
    if (problem) {
      setError(problem);
      return;
    }
    setError(null);
    const next = steps[stepIndex + 1];
    if (next) patch({ step: next });
  }

  function goBack() {
    setError(null);
    const previous = steps[stepIndex - 1];
    if (previous) patch({ step: previous });
  }

  function buildPayload(): WorkspaceCreatePayload {
    const base: WorkspaceCreatePayload = {
      name: form.name.trim(),
      default_branch: form.defaultBranch.trim() || "main",
    };
    if (usesSnapshot && form.snapshot.trim()) base.snapshot = form.snapshot.trim();
    if (form.runtime) base.runtime = form.runtime;
    if (form.source === "blank") return { ...base, source: "blank" };
    if (form.source === "template" && form.template) {
      return {
        ...base,
        source: "template",
        template_id: form.template.id,
        default_branch: form.template.default_branch,
      };
    }
    return { ...base, source: "repository", repo_url: form.repoUrl.trim() };
  }

  async function handleCreate() {
    const problem = steps.map(validate).find((item) => item !== null) ?? null;
    if (problem) {
      setError(problem);
      return;
    }
    setError(null);
    setSubmitting(true);
    let workspace: WorkspaceResponse;
    try {
      workspace = await createFn(buildPayload());
    } catch (caughtError) {
      setError(
        caughtError instanceof ApiError ? caughtError.message : "Unable to create workspace.",
      );
      setSubmitting(false);
      return;
    }

    let credential: RepositoryCredentialResponse | null = null;
    let postCreateError: string | null = null;
    const connecting = form.source === "repository" && form.useCredentials;
    if (connecting) {
      try {
        await connectRepository(workspace.id, {
          repo_url: form.repoUrl.trim(),
          provider: form.provider,
          auth_type: form.authType,
          ...(form.authType === "personal_access_token"
            ? {
                token: form.token,
                ...(form.username.trim() ? { username: form.username.trim() } : {}),
              }
            : {}),
        });
        try {
          credential = await getRepositoryCredential(workspace.id);
        } catch {
          credential = null;
        }
        if (form.authType === "personal_access_token" && form.cloneAfterCreate) {
          await cloneRepository(workspace.id);
          // Prepare the toolchain from mise.toml; the Preview tab reports progress.
          void setupDevServer(workspace.id).catch(() => undefined);
        }
      } catch (caughtError) {
        postCreateError = `${
          caughtError instanceof ApiError ? caughtError.message : "Connecting the repository failed."
        } You can finish connecting from the Repository tab.`;
      }
    }
    setSubmitting(false);

    const needsDoneScreen =
      postCreateError !== null || (connecting && form.authType === "ssh_key");
    if (needsDoneScreen) {
      setOutcome({ workspace, credential, postCreateError });
    } else {
      onCreated(workspace);
    }
  }

  async function handleCloneNow() {
    if (!created) return;
    setSubmitting(true);
    setError(null);
    try {
      await cloneRepository(created.id);
      void setupDevServer(created.id).catch(() => undefined);
      setSubmitting(false);
      onCreated(created);
    } catch (caughtError) {
      setSubmitting(false);
      setError(caughtError instanceof ApiError ? caughtError.message : "Clone failed.");
    }
  }

  async function copyPublicKey() {
    const key = outcome?.credential?.public_key;
    if (!key) return;
    await navigator.clipboard?.writeText(key);
    setCopyLabel("Copied");
    window.setTimeout(() => setCopyLabel("Copy"), 1200);
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 px-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="create-workspace-title"
        className="max-h-[92vh] w-full max-w-2xl overflow-y-auto rounded-xl border border-line bg-panel p-6 shadow-lg"
      >
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <Box className="h-5 w-5" aria-hidden="true" />
            </div>
            <div>
              <h2 id="create-workspace-title" className="text-lg font-semibold text-ink">
                New workspace
              </h2>
              <p className="text-xs text-muted">{subtitle}</p>
            </div>
          </div>
          <button
            type="button"
            onClick={handleClose}
            aria-label="Close"
            className="rounded-md p-1 text-muted transition hover:bg-line hover:text-ink"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>

        {!outcome && (
          <ol className="mt-4 flex flex-wrap gap-2" aria-label="Steps">
            {steps.map((step, index) => {
              const active = step === form.step;
              const completed = index < stepIndex;
              return (
                <li key={step}>
                  <button
                    type="button"
                    disabled={!completed || submitting}
                    aria-current={active ? "step" : undefined}
                    onClick={() => {
                      setError(null);
                      patch({ step });
                    }}
                    className={`flex items-center gap-2 rounded-full border px-3 py-1.5 text-sm transition disabled:cursor-default ${
                      active
                        ? "border-ink bg-ink text-white"
                        : completed
                          ? "border-line bg-white text-body hover:bg-panel"
                          : "border-line bg-white text-muted"
                    }`}
                  >
                    <span className="text-xs font-semibold">{index + 1}</span>
                    {STEP_LABELS[step]}
                  </button>
                </li>
              );
            })}
          </ol>
        )}

        <div className="mt-5">
          {outcome ? (
            <DoneScreen
              outcome={outcome}
              copyLabel={copyLabel}
              onCopy={() => void copyPublicKey()}
              sshHelp={help.sshHelp}
            />
          ) : form.step === "source" ? (
            <div role="radiogroup" aria-label="Workspace source" className="grid gap-3 sm:grid-cols-3">
              {SOURCE_OPTIONS.map(({ value, title, body, Icon }) => {
                const selected = form.source === value;
                return (
                  <button
                    key={value}
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    onClick={() => patch({ source: value })}
                    className={`flex flex-col rounded-xl border bg-white p-4 text-left transition hover:border-primary ${
                      selected ? "border-ink ring-1 ring-ink" : "border-line"
                    }`}
                  >
                    <Icon className="h-5 w-5 text-body" aria-hidden="true" />
                    <span className="mt-3 font-semibold text-ink">{title}</span>
                    <span className="mt-1 text-sm text-body">{body}</span>
                  </button>
                );
              })}
            </div>
          ) : form.step === "details" ? (
            <div className="space-y-4">
              <TextInput
                label="Workspace name"
                value={form.name}
                onChange={(name) => patch({ name })}
                autoFocus
                placeholder="My workspace"
              />
              {form.source === "template" && (
                <TemplateGallery
                  templates={templates}
                  loading={templatesLoading}
                  error={templatesError}
                  selectedId={form.template?.id ?? null}
                  onRetry={() => setTemplatesAttempt((value) => value + 1)}
                  onSelect={(template) =>
                    patch({
                      template,
                      defaultBranch: template.default_branch,
                      name: form.name.trim() ? form.name : template.name,
                    })
                  }
                />
              )}
              {form.source === "repository" && (
                <TextInput
                  label="Repository URL"
                  value={form.repoUrl}
                  onChange={(repoUrl) => patch({ repoUrl })}
                  placeholder={help.urlPlaceholder}
                  mono
                  hint={
                    form.repoUrl.trim()
                      ? `Detected: ${providerLabel(detectProvider(form.repoUrl))}`
                      : "HTTPS for token access, SSH (git@host:owner/repo.git) for deploy keys."
                  }
                />
              )}
              <div className="grid gap-4 md:grid-cols-2">
                {form.source !== "template" && (
                  <TextInput
                    label="Default branch"
                    value={form.defaultBranch}
                    onChange={(defaultBranch) => patch({ defaultBranch })}
                  />
                )}
                {usesSnapshot && (
                  <TextInput
                    label="Daytona snapshot (optional)"
                    value={form.snapshot}
                    onChange={(snapshot) => patch({ snapshot })}
                  />
                )}
              </div>
              {runtimes.length > 0 && (
                <fieldset>
                  <legend className="text-sm font-medium text-ink">Runtime</legend>
                  <div className="mt-2 grid gap-2 md:grid-cols-2">
                    {runtimes.map((option) => (
                      <label
                        key={option.value}
                        className={`flex items-start gap-3 rounded-lg border px-3 py-2 text-sm ${
                          form.runtime === option.value
                            ? "border-primary bg-white"
                            : "border-line bg-white"
                        } ${option.available ? "cursor-pointer" : "cursor-not-allowed opacity-60"}`}
                      >
                        <input
                          type="radio"
                          name="runtime"
                          value={option.value}
                          checked={form.runtime === option.value}
                          disabled={!option.available}
                          onChange={() => patch({ runtime: option.value })}
                          className="mt-1"
                        />
                        <span>
                          <span className="block font-medium text-ink">{option.label}</span>
                          <span className="block text-xs text-muted">
                            {option.reason ?? option.description}
                          </span>
                        </span>
                      </label>
                    ))}
                  </div>
                  {runtimes.every((option) => !option.available) && (
                    <button
                      type="button"
                      onClick={() => setHealthAttempt((value) => value + 1)}
                      className="mt-2 text-xs font-medium text-primary hover:underline"
                    >
                      Check again
                    </button>
                  )}
                </fieldset>
              )}
            </div>
          ) : form.step === "access" ? (
            <div className="space-y-4">
              <ProviderAuthSelector
                repoUrl={form.repoUrl}
                provider={form.provider}
                onProviderChange={(provider) => patch({ provider })}
                authType={form.authType}
                onAuthTypeChange={(authType) => patch({ authType })}
                token={form.token}
                onTokenChange={(token) => patch({ token })}
                username={form.username}
                onUsernameChange={(username) => patch({ username })}
                disabled={!form.useCredentials}
              />
              <label className="flex items-center gap-2 text-sm text-body">
                <input
                  type="checkbox"
                  checked={!form.useCredentials}
                  onChange={(event) => patch({ useCredentials: !event.target.checked })}
                />
                Skip for now — I&apos;ll add credentials from the Repository tab.
              </label>
            </div>
          ) : (
            <div className="space-y-4">
              <dl className="grid gap-3 rounded-md border border-line bg-white p-4 text-sm sm:grid-cols-2">
                <ReviewRow label="Source" value={sourceTitle(form.source)} />
                <ReviewRow label="Name" value={form.name.trim()} />
                <ReviewRow label="Default branch" value={form.defaultBranch.trim() || "main"} />
                {form.source === "template" && (
                  <ReviewRow label="Template" value={form.template?.name ?? "—"} />
                )}
                {form.source === "repository" && (
                  <>
                    <ReviewRow label="Repository" value={form.repoUrl.trim()} mono />
                    <ReviewRow label="Provider" value={providerLabel(resolvedProvider)} />
                    <ReviewRow
                      label="Access"
                      value={form.useCredentials ? authTypeLabel(form.authType) : "Skipped"}
                    />
                  </>
                )}
                {form.runtime && <ReviewRow label="Runtime" value={runtimeLabel(form.runtime)} />}
                {usesSnapshot && form.snapshot.trim() && (
                  <ReviewRow label="Snapshot" value={form.snapshot} mono />
                )}
              </dl>
              {form.source === "repository" &&
                form.useCredentials &&
                form.authType === "personal_access_token" && (
                  <label className="flex items-center gap-2 text-sm text-body">
                    <input
                      type="checkbox"
                      checked={form.cloneAfterCreate}
                      onChange={(event) => patch({ cloneAfterCreate: event.target.checked })}
                    />
                    Clone the repository right away
                  </label>
                )}
              {form.source === "repository" && form.useCredentials && form.authType === "ssh_key" && (
                <p className="text-sm text-body">
                  A deploy key is generated next. The repository is cloned after you add it on{" "}
                  {providerLabel(resolvedProvider)}.
                </p>
              )}
            </div>
          )}
        </div>

        {error && (
          <div className="mt-4 rounded-md border border-danger/30 bg-[#fff5f2] px-3 py-2 text-sm text-danger">
            {error}
          </div>
        )}

        <div className="mt-6 flex items-center justify-between gap-2">
          <div>
            {!outcome && stepIndex > 0 && (
              <button
                type="button"
                onClick={goBack}
                disabled={submitting}
                className="rounded-lg px-4 py-2 text-sm text-body hover:bg-line disabled:opacity-60"
              >
                Back
              </button>
            )}
          </div>
          <div className="flex gap-2">
            {outcome ? (
              <>
                {outcome.credential?.auth_type === "ssh_key" && !outcome.postCreateError && (
                  <button
                    type="button"
                    onClick={() => void handleCloneNow()}
                    disabled={submitting}
                    className="rounded-lg border border-line bg-white px-4 py-2 text-sm font-medium text-ink hover:bg-panel disabled:opacity-60"
                  >
                    {submitting ? "Cloning…" : "Clone now"}
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => onCreated(outcome.workspace)}
                  disabled={submitting}
                  className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-active disabled:opacity-60"
                >
                  Continue
                </button>
              </>
            ) : form.step === "review" ? (
              <button
                type="button"
                onClick={() => void handleCreate()}
                disabled={submitting}
                className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-active disabled:opacity-60"
              >
                {submitting ? "Creating…" : "Create workspace"}
              </button>
            ) : (
              <button
                type="button"
                onClick={goNext}
                className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-active"
              >
                Next
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function sourceTitle(source: WorkspaceSource | null): string {
  return SOURCE_OPTIONS.find((option) => option.value === source)?.title ?? "—";
}

function ReviewRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs uppercase tracking-wide text-muted">{label}</dt>
      <dd className={`mt-0.5 break-all text-ink ${mono ? "font-mono text-xs" : ""}`}>{value}</dd>
    </div>
  );
}

function DoneScreen({
  outcome,
  copyLabel,
  onCopy,
  sshHelp,
}: {
  outcome: Outcome;
  copyLabel: string;
  onCopy: () => void;
  sshHelp: string;
}) {
  const key = outcome.credential?.public_key ?? null;
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 text-sm text-signal">
        <Check className="h-4 w-4" aria-hidden="true" />
        Workspace “{outcome.workspace.name}” created.
      </div>
      {outcome.postCreateError && (
        <div className="rounded-md border border-warn/30 bg-[#fdf6ec] px-3 py-2 text-sm text-warn">
          {outcome.postCreateError}
        </div>
      )}
      {key && (
        <div className="rounded-md border border-line bg-white p-4">
          <h3 className="font-semibold text-ink">Deploy key generated</h3>
          <p className="mt-1 text-sm text-body">{sshHelp}</p>
          <pre className="mt-3 max-h-40 overflow-auto whitespace-pre-wrap break-all rounded-md bg-canvas p-3 font-mono text-xs text-ink">
            {key}
          </pre>
          <button
            type="button"
            onClick={onCopy}
            className="mt-2 rounded-md border border-line bg-white px-3 py-1.5 text-xs font-semibold text-ink hover:bg-panel"
          >
            {copyLabel}
          </button>
        </div>
      )}
    </div>
  );
}
