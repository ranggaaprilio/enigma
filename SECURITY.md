# Security Policy

Enigma executes AI agents and user-supplied code inside sandboxes, stores git credentials and
LLM API keys, and, when self-hosted, runs next to an OpenSandbox server that holds the container
daemon socket. We take reports about any of this seriously.

## Supported versions

Enigma has not yet cut a stable release. Security fixes land on the `main` branch; please run a
recent commit.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through
[GitHub's private vulnerability reporting](https://github.com/ranggaaprilio/enigma/security/advisories/new)
for this repository. If that is unavailable to you, contact a maintainer directly through GitHub.

Include as much of the following as you can:

- A description of the issue and its impact.
- The component involved (backend API, preview proxy, OpenSandbox or Daytona sandbox provider,
  frontend, deployment manifests).
- Steps to reproduce or a proof of concept.
- The commit or version you tested against and your sandbox runtime.

You should receive an acknowledgement within a few days. We will keep you informed of progress
and credit you in the fix unless you prefer otherwise.

## Scope

In scope:

- Sandbox escape or cross-sandbox access (OpenSandbox provider: the create request, the container
  floor, per-sandbox execd tokens, the egress policy and preview paths; Daytona provider: network
  policy and preview URLs).
- Authentication, session, and authorisation flaws (projects, teams, workspaces, owner-only
  settings).
- Exposure of stored secrets (workspace environment variables, git tokens, SSH deploy keys, LLM
  API keys).
- Agent tool confinement bypasses (path escapes, command denylist bypasses).
- Server-side request forgery through the preview proxy or past the sandbox egress policy.

Out of scope:

- Vulnerabilities in third-party dependencies that are not exploitable through Enigma. Report
  those upstream, but feel free to let us know so we can upgrade.
- Issues that require a deliberately insecure configuration, such as mounting the daemon socket
  into a sandbox or disabling the command denylist, beyond what the documentation describes.
- Denial of service through resource exhaustion inside a sandbox that stays within the configured
  limits.

## Deployment hardening

Operators should read the security sections of
[`docs/self-host-opensandbox.md`](docs/self-host-opensandbox.md#security-first) and
[`deploy/README.md`](deploy/README.md) before exposing an installation. In short: run the
OpenSandbox server, which holds the daemon socket, on a dedicated machine or VM reachable only
over a private network; set `OPENSANDBOX_API_KEY` and `OPENSANDBOX_EXECD_TOKEN_KEY` (each
sandbox gets its own execd token); keep `[egress] mode = "dns+nft"` in the server config; set
`METRICS_TOKEN`; use strong `AUTH_SECRET_KEY` and `WORKSPACE_ENV_ENCRYPTION_KEY` values; restrict
sandbox egress; and serve the API over HTTPS.
