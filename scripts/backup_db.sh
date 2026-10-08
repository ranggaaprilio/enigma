#!/usr/bin/env bash
#
# Back up the local enigma Postgres database to a custom-format dump (pg_dump -Fc).
#
# The dump holds every user, workspace and run, plus credentials encrypted with
# WORKSPACE_ENV_ENCRYPTION_KEY. Restoring it without that same key leaves them unreadable, so
# keep the key with the backup (but never in the same place), and keep both access-controlled.
#
# Usage:
#   ./scripts/backup_db.sh                          # -> backups/<db>-<UTC timestamp>.dump
#   ./scripts/backup_db.sh --out /path/to/file.dump
#   ./scripts/backup_db.sh --runtime docker
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

CONTAINER_RUNTIME="podman"
OUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --runtime) CONTAINER_RUNTIME="${2:?ERROR: --runtime requires a value (podman or docker)}"; shift 2 ;;
    --runtime=*) CONTAINER_RUNTIME="${1#*=}"; shift ;;
    --out) OUT="${2:?ERROR: --out requires a path}"; shift 2 ;;
    --out=*) OUT="${1#*=}"; shift ;;
    *) echo "ERROR: unknown argument: $1"; exit 1 ;;
  esac
done

if ! command -v "${CONTAINER_RUNTIME}" &>/dev/null; then
  echo "ERROR: '${CONTAINER_RUNTIME}' not found in PATH."
  exit 1
fi

POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-enigma-postgres-1}"
if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  set -a; source .env; set +a
fi
POSTGRES_USER="${POSTGRES_USER:-execute}"
POSTGRES_DB="${POSTGRES_DB:-execute_dashboard}"

# Owner-only from the first byte (directory and dump): the dump is as sensitive as the database.
umask 077
if [[ -z "${OUT}" ]]; then
  mkdir -p -m 700 backups
  OUT="backups/${POSTGRES_DB}-$(date -u +%Y%m%dT%H%M%SZ).dump"
fi
TMP="${OUT}.partial"
trap 'rm -f "${TMP}"' EXIT

echo "==> Dumping ${POSTGRES_CONTAINER} / ${POSTGRES_DB} -> ${OUT}"
${CONTAINER_RUNTIME} exec "${POSTGRES_CONTAINER}" \
  pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" --format=custom --no-owner >"${TMP}"
# Only a complete dump gets the final name, so a failed run never looks like a backup.
mv "${TMP}" "${OUT}"
trap - EXIT

echo "==> Done: ${OUT} ($(wc -c <"${OUT}" | tr -d ' ') bytes)"
