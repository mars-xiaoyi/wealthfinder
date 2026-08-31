#!/usr/bin/env bash
# Phase 0 — automated pytest suites. Run with the venv active.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

echo "=== Unit tests ==="
pytest

echo
echo "=== Live integration tests (real network, stub DB) ==="
pytest -m live -v
