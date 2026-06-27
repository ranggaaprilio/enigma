# Custom Daytona sandbox snapshot for Enigma previews testing.
#
# Why this exists:
#   Two runtime features bootstrap their tooling with a `curl … | sh` install:
#     - the dev-server preview (backend/app/services/dev_server.py) downloads `mise`
#       via `curl -fsSL https://mise.run | sh`, and
#     - the editor session (backend/app/services/editor.py) downloads `code-server`
#       via `curl -fsSL https://code-server.dev/install.sh | sh …`.
#   Inside the stock `daytonaio/sandbox` image those curls are reset by the sandbox
#   network (curl: (35) Recv failure: Connection reset by peer), so the binaries
#   never land on PATH and the launch scripts exit 127. Baking both into the image at
#   *build* time — when the build host has network — removes the runtime downloads
#   entirely: each launch script's `command -v …` succeeds and the curl branch is
#   skipped.
#
# Base image already provides: Linux shell, Git, Python 3, and the process/network
# utilities the backend relies on for commands and port detection.
ARG BASE_SNAPSHOT=daytonaio/sandbox:0.8.0
FROM ${BASE_SNAPSHOT}

# Pin for reproducible snapshots; bump deliberately. Empty = latest at build time.
ARG MISE_VERSION=

# Install mise for the sandbox user. The Docker build runs as the unprivileged
# `daytona` user (home /home/daytona) and cannot write to /usr/local/bin, so we use
# the installer's default location: $HOME/.local/bin/mise. That is exactly where the
# runtime launch script looks — it prepends $HOME/.local/bin to PATH before
# `command -v mise` — so the baked-in binary is found and the runtime curl is skipped.
RUN set -eux; \
    if [ -n "${MISE_VERSION}" ]; then export MISE_VERSION="${MISE_VERSION}"; fi; \
    curl -fsSL https://mise.run | sh; \
    "$HOME/.local/bin/mise" --version

# Install code-server for the sandbox user, using the exact same standalone install
# the editor launch script runs at runtime: `--method standalone --prefix $HOME/.local`
# drops the binary at $HOME/.local/bin/code-server (= /home/daytona/.local/bin), which
# is precisely where the launch script's `command -v code-server` looks. Baking it in
# means the runtime curl branch in editor.py is skipped and the editor never exits 127.
ARG CODE_SERVER_VERSION=
RUN set -eux; \
    if [ -n "${CODE_SERVER_VERSION}" ]; then VERSION_FLAG="--version ${CODE_SERVER_VERSION}"; else VERSION_FLAG=""; fi; \
    curl -fsSL https://code-server.dev/install.sh \
      | sh -s -- --method standalone --prefix "$HOME/.local" ${VERSION_FLAG}; \
    "$HOME/.local/bin/code-server" --version

# Install GitHub Spec Kit (the `specify` CLI) for Spec-Driven Development.
# Same rationale as mise/code-server above: the install needs network, but the sandbox
# runtime egress is blocked. Bake the CLI in at build time; `specify init` afterwards
# scaffolds .specify/ and specs/ OFFLINE from templates bundled in the package, so the
# SDD workflow (docs/improvement-epics/01-spec-driven-development.md) works at runtime.
# uv tool install drops the `specify` entry point into $HOME/.local/bin, which is exactly
# where the runtime PATH (set below) looks. Pin SPEC_KIT_REF to a tagged release for
# reproducible snapshots; empty = latest at build time.
ARG SPEC_KIT_REF=
RUN set -eux; \
    curl -fsSL https://astral.sh/uv/install.sh | sh; \
    if [ -n "${SPEC_KIT_REF}" ]; then \
      SRC="git+https://github.com/github/spec-kit.git@${SPEC_KIT_REF}"; \
    else \
      SRC="git+https://github.com/github/spec-kit.git"; \
    fi; \
    "$HOME/.local/bin/uv" tool install specify-cli --from "${SRC}"; \
    "$HOME/.local/bin/specify" check || true

# Put mise and code-server on PATH for non-login shells too, so tools resolved by
# `mise install` and the code-server binary are visible without sourcing a profile.
# Home is /home/daytona (daytona_sandbox_home).
ENV PATH="/home/daytona/.local/bin:${PATH}"

# OPTIONAL — pre-warm the toolchain so the runtime `mise install` needs no network.
# The preview's `mise install` step still downloads runtimes (node, python, …) from
# the internet. If your sandbox egress is also blocked, COPY your project's mise
# config into the image and install the tools here so they're baked in:
#
#   COPY mise.toml /opt/enigma-tools/mise.toml
#   RUN cd /opt/enigma-tools && mise trust && mise install
#
# (Tools land in mise's shared cache and are reused by every sandbox.)
