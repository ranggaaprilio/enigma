# Custom Daytona sandbox snapshot for Enigma previews.
#
# Why this exists:
#   The dev-server preview (backend/app/services/dev_server.py) bootstraps `mise`
#   at runtime with `curl -fsSL https://mise.run | sh`. Inside the stock
#   `daytonaio/sandbox` image that curl is reset by the sandbox network
#   (curl: (35) Recv failure: Connection reset by peer), so mise never lands on
#   PATH and setup exits 127. Baking mise into the image at *build* time — when the
#   build host has network — removes the runtime download entirely: the launch
#   script's `command -v mise` succeeds and the curl branch is skipped.
#
# Base image already provides: Linux shell, Git, Python 3, code-server, and the
# process/network utilities the backend relies on for commands and port detection.
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

# Put mise on PATH for non-login shells too, so tools resolved by `mise install` are
# visible without sourcing a profile. Home is /home/daytona (daytona_sandbox_home).
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
