#!/usr/bin/env bash
# Backup AgentFlow PostgreSQL data from the production compose stack.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
mkdir -p "$BACKUP_DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

POSTGRES_USER="${POSTGRES_USER:-admin}"
POSTGRES_DB="${POSTGRES_DB:-agentflow_db}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/agentflow-$STAMP.sql.gz"

docker compose -f compose.prod.yaml exec -T postgres-vector \
  pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$OUT"

echo "Backup written to $OUT"
