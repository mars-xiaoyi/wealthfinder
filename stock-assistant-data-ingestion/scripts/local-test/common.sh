#!/usr/bin/env bash
# Shared helpers for scripts/local-test/*.sh — sourced, not run directly.
set -euo pipefail

BASE_URL="${SADI_BASE_URL:-http://localhost:8000}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

psql_sadi() {
  (cd "$PROJECT_ROOT" && docker compose exec -T postgres psql -U postgres -d sadi "$@")
}

redis_sadi() {
  (cd "$PROJECT_ROOT" && docker compose exec -T redis redis-cli "$@")
}

compose() {
  (cd "$PROJECT_ROOT" && docker compose "$@")
}
