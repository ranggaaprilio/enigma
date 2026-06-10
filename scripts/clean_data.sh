#!/usr/bin/env bash
#
# Clean up Redis and Postgres data in-place for the local enigma stack.
#
# - Redis:    FLUSHALL (wipes all keys)
# - Postgres: DROP & recreate the public schema, then re-run alembic migrations
#             so the app comes back to a clean, empty, migrated database.
#
# Containers stay running the whole time. Credentials are read from .env
# (falling back to docker-compose.yml defaults).
#
# Usage:
#   ./scripts/clean_data.sh           # prompts for confirmation
#   ./scripts/clean_data.sh --yes     # skip the confirmation prompt
#
set -euo pipefail

# --- locate repo root (parent of this script's dir) -------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

# --- container names --------------------------------------------------------
REDIS_CONTAINER="${REDIS_CONTAINER:-enigma-redis-1}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-enigma-postgres-1}"
BACKEND_CONTAINER="${BACKEND_CONTAINER:-enigma-backend-1}"

# --- load Postgres credentials from .env (with compose defaults) ------------
if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  set -a; source .env; set +a
fi
POSTGRES_USER="${POSTGRES_USER:-execute}"
POSTGRES_DB="${POSTGRES_DB:-execute_dashboard}"

# --- confirmation -----------------------------------------------------------
if [[ "${1:-}" != "--yes" && "${1:-}" != "-y" ]]; then
  echo "This will PERMANENTLY DELETE all data in:"
  echo "  - Redis:    ${REDIS_CONTAINER} (FLUSHALL)"
  echo "  - Postgres: ${POSTGRES_CONTAINER} / db '${POSTGRES_DB}' (drop schema + re-migrate)"
  read -r -p "Continue? [y/N] " reply
  [[ "${reply}" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
fi

# --- redis ------------------------------------------------------------------
echo "==> Flushing Redis (${REDIS_CONTAINER})..."
podman exec "${REDIS_CONTAINER}" redis-cli FLUSHALL
echo "    DBSIZE now: $(podman exec "${REDIS_CONTAINER}" redis-cli DBSIZE)"

# --- postgres ---------------------------------------------------------------
echo "==> Dropping & recreating public schema (${POSTGRES_CONTAINER} / ${POSTGRES_DB})..."
podman exec "${POSTGRES_CONTAINER}" psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -v ON_ERROR_STOP=1 -c "
  DROP SCHEMA public CASCADE;
  CREATE SCHEMA public;
  GRANT ALL ON SCHEMA public TO ${POSTGRES_USER};
  GRANT ALL ON SCHEMA public TO public;
"

# --- re-run migrations ------------------------------------------------------
echo "==> Re-applying Alembic migrations (${BACKEND_CONTAINER})..."
podman exec "${BACKEND_CONTAINER}" sh -lc "cd /app && uv run alembic upgrade head"

echo "==> Done. Redis flushed; Postgres reset to a clean, migrated schema."
