#!/usr/bin/env bash
# Start a research control loop and its workers in ONE verified Python runtime.
# Reuse an existing WSL numerical environment when supplied, without pip installs.
# Do not launch this concurrently with another Supervisor.
# Usage: bash scripts/run_project_science_research.sh <project> [budget] [existing_python]
# Also accepts DANUS_SCIENCE_PY=/absolute/path/to/existing/bin/python.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${1:-}"
BUDGET="${2:-10000000}"
if [[ ! "$PROJECT" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]]; then
  echo "ERROR: valid project name required" >&2; exit 2
fi
[[ "$BUDGET" =~ ^[0-9]+$ ]] || { echo "ERROR: invalid token budget" >&2; exit 2; }
. "$ROOT/scripts/env.sh"
PY="${3:-${DANUS_SCIENCE_PY:-$ROOT/runtime/science-envs/$PROJECT/bin/python}}"
[[ "$PY" = /* ]] || { echo "ERROR: science Python path must be absolute" >&2; exit 3; }
[[ -x "$PY" ]] || { echo "ERROR: interpreter not executable: $PY" >&2; exit 3; }
# Extend the numerical interpreter's search path only with Danus's trusted,
# existing control-plane site-packages. No pip install and no package copying.
CONTROL_PY="$ROOT/runtime/venv/bin/python"
[[ -x "$CONTROL_PY" ]] || { echo "ERROR: Danus control venv missing" >&2; exit 3; }
CONTROL_SITE="$("$CONTROL_PY" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
[[ -d "$CONTROL_SITE" ]] || { echo "ERROR: control site-packages missing" >&2; exit 3; }
export DANUS_TRUSTED_CONTROL_SITE="$CONTROL_SITE"
export PYTHONPATH="$ROOT:$CONTROL_SITE"
"$PY" - <<'PY'
import sys, numpy, scipy, torch
import danus.orchestration
import danus.gateway
import openai
import anthropic
x = torch.tensor(2.0, requires_grad=True)
(x*x).backward()
if x.grad.item() != 4.0:
    raise SystemExit("autograd smoke check failed")
print("Research interpreter ready:", sys.executable, "torch:", torch.__version__,
      "CUDA built:", torch.version.cuda, "GPU available:", torch.cuda.is_available())
PY
export PATH="$(dirname "$PY"):$PATH"
export DANUS_PY="$PY"
export DANUS_SCIENTIFIC_WORKSPACE="$ROOT/runtime/workspaces/$PROJECT"
mkdir -p "$ROOT/runtime/logs"
echo "[science] launching project=$PROJECT with interpreter=$PY"
echo "[science] control imports from $CONTROL_SITE; numerical packages from $PY; watchdog=${DANUS_DEADLOCK_IDLE_SECONDS:-45}s"
cd "$ROOT"
set -o pipefail
"$PY" -m danus.orchestration research "$PROJECT" --token-budget "$BUDGET" 2>&1 | tee -a "$ROOT/runtime/logs/$PROJECT.log"
