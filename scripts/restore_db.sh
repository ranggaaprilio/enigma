#!/usr/bin/env bash
#
# Restore a dump made by backup_db.sh.
#
# By default it restores into a scratch database (<db>_restore_check), which leaves the live one
# alone. That is how a backup gets tested: restore it, then look at alembic_version and the row
# counts this script prints. The dump is validated before anything is dropped. The only other
# allowed target is the live database itself, and that takes --replace-live (stop backend and
# worker first); --yes never implies it.
#
# Usage:
#   ./scripts/restore_db.sh backups/execute_dashboard-20261008T000000Z.dump
#   ./scripts/restore_db.sh <dump> --drop-after          # test, then clean up (also on failure)
#   ./scripts/restore_db.sh <dump> --replace-live        # replace the live DB (prompts)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

CONTAINER_RUNTIME="podman"
DUMP=""
REPLACE_LIVE=""
CONFIRM_YES=""
DROP_AFTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --runtime) CONTAINER_RUNTIME="${2:?ERROR: --runtime requires a value (podman or docker)}"; shift 2 ;;
    --runtime=*) CONTAINER_RUNTIME="${1#*=}"; shift ;;
    --replace-live) REPLACE_LIVE="1"; shift ;;
    --yes|-y) CONFIRM_YES="1"; shift ;;
    --drop-after) DROP_AFTER="1"; shift ;;
    -*) echo "ERROR: unknown argument: $1"; exit 1 ;;
    *) DUMP="$1"; shift ;;
  esac
done

[[ -n "${DUMP}" && -f "${DUMP}" ]] || { echo "ERROR: pass the path of an existing dump file."; exit 1; }
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
[[ "${POSTGRES_DB}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || { echo "ERROR: invalid POSTGRES_DB: ${POSTGRES_DB}"; exit 1; }
# Only ever this app's scratch copy or (explicitly) its live database -- never another database
# on the same instance.
if [[ "${REPLACE_LIVE}" == "1" ]]; then
  TARGET="${POSTGRES_DB}"
  [[ -z "${DROP_AFTER}" ]] || { echo "ERROR: --drop-after would drop the live database."; exit 1; }
else
  TARGET="${POSTGRES_DB}_restore_check"
fi

psql_admin() {
  ${CONTAINER_RUNTIME} exec -i "${POSTGRES_CONTAINER}" \
    psql -U "${POSTGRES_USER}" -d postgres -v ON_ERROR_STOP=1 -qAt "$@"
}

# Validate before dropping anything: a truncated or wrong file must fail here, not after the
# target is gone.
echo "==> Validating ${DUMP}"
${CONTAINER_RUNTIME} exec -i "${POSTGRES_CONTAINER}" pg_restore --list <"${DUMP}" >/dev/null \
  || { echo "ERROR: ${DUMP} is not a readable pg_dump custom-format archive."; exit 1; }

if [[ "${REPLACE_LIVE}" == "1" ]]; then
  echo "!! '${TARGET}' is the LIVE database. Stop backend and worker first; take a fresh"
  echo "!! ./scripts/backup_db.sh before replacing it."
  # Replacing live data always asks, even with --yes.
  read -r -p "Type the database name to replace it: " reply
  [[ "${reply}" == "${TARGET}" ]] || { echo "Aborted."; exit 1; }
elif [[ -z "${CONFIRM_YES}" ]]; then
  echo "This will DROP and recreate scratch database '${TARGET}' in ${POSTGRES_CONTAINER}, then restore ${DUMP}."
  read -r -p "Continue? [y/N] " reply
  [[ "${reply}" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
fi

if [[ "${DROP_AFTER}" == "1" ]]; then
  # A failed restore must not leave a copy of production data lying around.
  trap 'psql_admin -c "DROP DATABASE IF EXISTS \"${TARGET}\" WITH (FORCE);" || true' EXIT
fi

echo "==> Recreating database ${TARGET}"
psql_admin -c "DROP DATABASE IF EXISTS \"${TARGET}\" WITH (FORCE);"
psql_admin -c "CREATE DATABASE \"${TARGET}\" OWNER \"${POSTGRES_USER}\";"

echo "==> Restoring ${DUMP}"
${CONTAINER_RUNTIME} exec -i "${POSTGRES_CONTAINER}" \
  pg_restore -U "${POSTGRES_USER}" -d "${TARGET}" --no-owner --exit-on-error <"${DUMP}"

echo "==> Verifying ${TARGET}"
query() {
  ${CONTAINER_RUNTIME} exec "${POSTGRES_CONTAINER}" \
    psql -U "${POSTGRES_USER}" -d "${TARGET}" -v ON_ERROR_STOP=1 -qAt -c "$1"
}
echo "    alembic_version: $(query 'SELECT version_num FROM alembic_version')"
for table in users workspaces agent_runs agent_events command_runs notifications; do
  echo "    ${table}: $(query "SELECT count(*) FROM ${table}") rows"
done

if [[ "${DROP_AFTER}" == "1" ]]; then
  echo "==> Restore verified; dropping ${TARGET}."
else
  echo "==> Done: restored into ${TARGET}."
fi
