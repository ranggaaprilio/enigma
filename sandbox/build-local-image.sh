#!/usr/bin/env bash
# Build the Enigma sandbox image for the local Docker runtime (runtime="docker").
#
# Uses the same Dockerfile as the Daytona snapshot (daytona/build-snapshot.sh) so the two
# providers run an identical toolchain. Needs network from THIS machine at build time only
# (mise.run, code-server.dev, astral.sh, github.com).
#
# Usage:
#   ./sandbox/build-local-image.sh                       # tags enigma-sandbox:latest
#   IMAGE=enigma-sandbox:dev ./sandbox/build-local-image.sh
#   CONTAINER_CLI=podman ./sandbox/build-local-image.sh  # default: podman if present, else docker
#
# Then set DOCKER_SANDBOX_IMAGE to the tag (default enigma-sandbox:latest) in .env.
set -euo pipefail

IMAGE="${IMAGE:-enigma-sandbox:latest}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "${CONTAINER_CLI:-}" ]]; then
  if command -v podman >/dev/null 2>&1; then CONTAINER_CLI=podman; else CONTAINER_CLI=docker; fi
fi

"${CONTAINER_CLI}" build -t "${IMAGE}" -f "${DIR}/Dockerfile" "${DIR}"

# The sandboxes' bridge network. docker-compose.sandbox.yml attaches the preview proxy to it
# as an external network, so it must exist before `compose up`; DockerRuntime adopts it.
NETWORK="${DOCKER_SANDBOX_NETWORK:-enigma-sandboxes}"
if ! "${CONTAINER_CLI}" network inspect "${NETWORK}" >/dev/null 2>&1; then
  "${CONTAINER_CLI}" network create --driver bridge --label enigma.managed=1 "${NETWORK}"
fi

echo
echo "Built ${IMAGE} and network ${NETWORK}. Set DOCKER_SANDBOX_IMAGE=${IMAGE} in .env (that is already the default)."
