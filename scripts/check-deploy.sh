#!/usr/bin/env bash
# Post-deploy smoke checks for compose.prod.yaml on the same host as Docker.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

COMPOSE_FILE="${COMPOSE_FILE:-compose.prod.yaml}"

echo "== docker compose ps ($COMPOSE_FILE) =="
docker compose -f "$COMPOSE_FILE" ps

echo ""
echo "== health (expects api listening on 127.0.0.1:8000 after prod compose) =="
curl -sfS --max-time 5 http://127.0.0.1:8000/healthz | jq . 2>/dev/null || curl -sfS --max-time 5 http://127.0.0.1:8000/healthz

echo ""
echo "== readiness =="
curl -sfS --max-time 15 http://127.0.0.1:8000/readyz | jq . 2>/dev/null || curl -sfS --max-time 15 http://127.0.0.1:8000/readyz

echo ""
echo "== frontend root (HTTP 200 expected) =="
curl -sfS -o /dev/null -w "HTTP %{http_code}\n" --max-time 10 http://127.0.0.1:8501/

echo ""
echo "OK: basic checks passed."
