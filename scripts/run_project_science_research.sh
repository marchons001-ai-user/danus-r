#!/usr/bin/env bash
# Start a research control loop and its scientific workers in the SAME isolated
# Python runtime. Do not launch this concurrently with another Supervisor.
# Usage: bash scripts/run_project_science_research.sh <project> [token_budget]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${1:-}"
BUDGET="${2:-10000000}"
if [[ ! "$PROJECT" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]]; then
  echo "ERROR: valid project name required" >&2; exit 2
fi
[[ "$BUDGET" =~ ^[0-9]+$ ]] || { echo "ERROR: invalid token budget" >&2; exit 2; }
. "$ROOT/scripts/env.sh"
PY="$ROOT/runtime/science-envs/$PROJECT/bin/python"
[[ -x "$PY" ]] || { echo "ERROR: science env missing; first run provision_project_science_env.sh" >&2; exit 3; }
"$PY" -c 'import danus, numpy, scipy, torch; print("Research interpreter ready:", __import__("sys").executable, "torch:", torch.__version__)'
export PATH="$(dirname "$PY"):$PATH"
export DANUS_PY="$PY"
export DANUS_SCIENTIFIC_WORKSPACE="$ROOT/runtime/workspaces/$PROJECT"
mkdir -p "$ROOT/runtime/logs"
echo "[science] launching project=$PROJECT with interpreter=$PY watchdog=${DANUS_DEADLOCK_IDLE_SECONDS:-45}s"
cd "$ROOT"
set -o pipefail
"$PY" -m danus.orchestration research "$PROJECT" --token-budget "$BUDGET" 2>&1 | tee -a "$ROOT/runtime/logs/$PROJECT.log"
