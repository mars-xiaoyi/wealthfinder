#!/usr/bin/env bash
# Run SAPI's live integration suite from a clean checkout: creates .venv and installs the test
# requirements when needed, then runs `pytest -m live`. Extra arguments go to pytest, e.g.
#   scripts/test-live.sh -k reclaim
#   scripts/test-live.sh -m "live and not vertex"   # skip the cases that call Vertex AI
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

command -v uv >/dev/null || { echo "uv is not installed: https://docs.astral.sh/uv/" >&2; exit 1; }
# The suite starts its own Postgres and Redis containers.
docker info >/dev/null 2>&1 || { echo "Docker is not running." >&2; exit 1; }

[ -x .venv/bin/python ] || uv venv
# shellcheck source=/dev/null
source .venv/bin/activate
uv pip install -q -r requirements-test.txt

# The vertex cases call the real Vertex AI with the developer's project and application default
# credentials; the project falls back to gcloud's default one.
if [ -z "${VERTEX_AI_PROJECT:-}" ] && command -v gcloud >/dev/null; then
  VERTEX_AI_PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
  [ "$VERTEX_AI_PROJECT" = "(unset)" ] && VERTEX_AI_PROJECT=""
  export VERTEX_AI_PROJECT
fi
if [ -z "${VERTEX_AI_PROJECT:-}" ] \
  || ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
  echo "warning: no Vertex AI project or application default credentials, so the vertex cases" \
    "will fail. Run 'gcloud auth application-default login' and" \
    "'gcloud config set project <id>' (or set VERTEX_AI_PROJECT)," \
    "or pass -m \"live and not vertex\"." >&2
fi

pytest -m live "$@"
