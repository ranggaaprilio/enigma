#!/usr/bin/env bash
# Build and publish the custom Enigma sandbox snapshot to Daytona.
#
# Prereqs:
#   - Daytona CLI installed and authenticated:  daytona login
#   - Network access from THIS machine to https://mise.run (build-time only).
#
# Usage:
#   ./daytona/build-snapshot.sh                 # builds snapshot "enigma-mise"
#   SNAPSHOT_NAME=enigma-mise-1 ./daytona/build-snapshot.sh
#
# After it succeeds, set in your .env:
#   DAYTONA_DEFAULT_SNAPSHOT=<the name printed below>
# then recreate any existing sandboxes so they pick up the new image.
set -euo pipefail

SNAPSHOT_NAME="${SNAPSHOT_NAME:-enigma-base}"
DOCKERFILE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Daytona snapshots must be linux/amd64. The CLI builds the image and parses the
# Dockerfile's COPY/ADD into the build context automatically.
daytona snapshot create "${SNAPSHOT_NAME}" \
  --dockerfile "${DOCKERFILE_DIR}/Dockerfile" \
  --context "${DOCKERFILE_DIR}" \
  --cpu 2 --memory 4 --disk 10

echo
echo "Snapshot '${SNAPSHOT_NAME}' created."
echo "Now set DAYTONA_DEFAULT_SNAPSHOT=${SNAPSHOT_NAME} in your .env and restart the backend."
