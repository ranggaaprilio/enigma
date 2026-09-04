import type { GitProvider, GitProviderChoice, RepositoryAuthType } from "./api";

export type ParsedGitRemote = {
  scheme: "https" | "ssh";
  host: string;
  /** `owner/repo` without a leading slash or `.git` suffix. */
  path: string;
};

function cleanPath(path: string): string {
  return path.replace(/^\/+/, "").replace(/\/+$/, "").replace(/\.git$/, "");
}

/** Parse `https://host/owner/repo(.git)`, `ssh://git@host[:port]/owner/repo` or `git@host:owner/repo`. */
export function parseGitRemote(url: string): ParsedGitRemote | null {
  const trimmed = url.trim();
  if (!trimmed) return null;
  const scp = /^git@([^:/\s@]+):([^\s]+)$/.exec(trimmed);
  if (scp) {
    const path = cleanPath(scp[2]);
    return path ? { scheme: "ssh", host: scp[1].toLowerCase(), path } : null;
  }
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return null;
  }
  let scheme: ParsedGitRemote["scheme"];
  if (parsed.protocol === "https:") scheme = "https";
  else if (parsed.protocol === "ssh:") scheme = "ssh";
  else return null;
  const host = parsed.hostname.toLowerCase();
  const path = cleanPath(parsed.pathname);
  if (!host || !path) return null;
  return { scheme, host, path };
}

function providerForHost(host: string): GitProvider {
  const bare = host.replace(/^www\./, "");
  if (bare === "github.com") return "github";
  if (bare === "gitlab.com") return "gitlab";
  if (bare === "bitbucket.org") return "bitbucket";
  return "generic";
}

/** Best-effort provider detection from the URL host; `generic` when unsure. */
export function detectProvider(url: string): GitProvider {
  const remote = parseGitRemote(url);
  return remote ? providerForHost(remote.host) : "generic";
}

export function resolveProvider(url: string, choice: GitProviderChoice): GitProvider {
  return choice === "auto" ? detectProvider(url) : choice;
}

export function providerLabel(provider: GitProvider): string {
  switch (provider) {
    case "github":
      return "GitHub";
    case "gitlab":
      return "GitLab";
    case "bitbucket":
      return "Bitbucket";
    default:
      return "Git host";
  }
}

export function providerHost(provider: GitProvider, url?: string | null): string {
  switch (provider) {
    case "github":
      return "github.com";
    case "gitlab":
      return parseGitRemote(url ?? "")?.host ?? "gitlab.com";
    case "bitbucket":
      return "bitbucket.org";
    default:
      return parseGitRemote(url ?? "")?.host ?? "your git host";
  }
}

/** GitLab calls them merge requests; everyone else says pull request. */
export function prNoun(provider: GitProvider): "pull request" | "merge request" {
  return provider === "gitlab" ? "merge request" : "pull request";
}

export function authTypeLabel(authType: RepositoryAuthType): string {
  return authType === "ssh_key" ? "SSH deploy key" : "Access token";
}

export type ProviderHelp = {
  tokenLabel: string;
  tokenHelp: string;
  /** Bitbucket app passwords authenticate as the real account, so the username is required. */
  requiresUsername: boolean;
  sshHelp: string;
  urlPlaceholder: string;
};

export const PROVIDER_HELP: Record<GitProvider, ProviderHelp> = {
  github: {
    tokenLabel: "Personal access token",
    tokenHelp:
      "Create a fine-grained personal access token with Contents: read and write (and Pull " +
      "requests: read and write to open pull requests) at github.com/settings/tokens.",
    requiresUsername: false,
    sshHelp:
      "Add the public key as a deploy key with write access under the repository's Settings › " +
      "Deploy keys.",
    urlPlaceholder: "https://github.com/org/repo",
  },
  gitlab: {
    tokenLabel: "Access token",
    tokenHelp:
      "Create a project access token with the read_repository and write_repository scopes " +
      "under Settings › Access Tokens. Self-hosted GitLab works too — pick GitLab explicitly.",
    requiresUsername: false,
    sshHelp:
      "Add the public key as a deploy key with write access under Settings › Repository › " +
      "Deploy keys.",
    urlPlaceholder: "https://gitlab.com/group/project",
  },
  bitbucket: {
    tokenLabel: "App password",
    tokenHelp:
      "Create an app password with Repositories: read and write under Personal settings › App " +
      "passwords, and enter your Bitbucket username alongside it.",
    requiresUsername: true,
    sshHelp:
      "Add the public key as an access key with write access under Repository settings › " +
      "Access keys.",
    urlPlaceholder: "https://bitbucket.org/workspace/repo",
  },
  generic: {
    tokenLabel: "Access token",
    tokenHelp:
      "Enter a token your git host accepts for HTTPS basic auth (and the username it expects, " +
      "if any). Pull requests are not available for generic hosts.",
    requiresUsername: false,
    sshHelp: "Add the public key as a deploy key with write access in your git host's settings.",
    urlPlaceholder: "https://git.example.com/org/repo.git or git@git.example.com:org/repo.git",
  },
};
