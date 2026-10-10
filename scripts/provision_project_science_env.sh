#!/usr/bin/env bash
# Host-operated, one-time setup for a project-specific numerical Python runtime.
# Does NOT approve contracts, resolve escalations, or retry an Assignment.
# Usage: bash scripts/provision_project_science_env.sh <project> [cuda|cpu]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${1:-}"
BACKEND="${2:-cuda}"
if [[ ! "$PROJECT" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]]; then
  echo "ERROR: supply an existing project name" >&2; exit 2
fi
case "$BACKEND" in cuda|cpu) ;; *) echo "ERROR: backend must be cuda or cpu" >&2; exit 2 ;; esac
[[ -d "$ROOT/runtime/projects/$PROJECT" ]] || { echo "ERROR: project missing: $PROJECT" >&2; exit 2; }
ENV_ROOT="$ROOT/runtime/science-envs/$PROJECT"
PY="$ENV_ROOT/bin/python"
mkdir -p "$ROOT/runtime/science-envs"
FROZEN_COUNT="$(/usr/bin/python3 - "$ROOT/runtime/projects" "$PY" <<'PY'
import json, sys
from pathlib import Path
root, target = Path(sys.argv[1]), str(Path(sys.argv[2]).absolute())
found = []
for path in root.glob("*/evidence/protocols/*.json"):
    try:
        p = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        continue
    if (p.get("execution_runtime") or {}).get("executable") == target:
        found.append(str(path))
for path in found:
    print("FROZEN:", path, file=sys.stderr)
print(len(found))
PY
)"
echo "[science-env] $PROJECT $BACKEND $PY"
if [[ -x "$PY" ]] && "$PY" - "$BACKEND" <<'PY'
import sys
try:
    import numpy, scipy, torch
    assert numpy.__version__ == "1.26.4"
    assert scipy.__version__ == "1.14.1"
    assert torch.__version__.startswith("2.5.1")
    assert bool(torch.version.cuda) == (sys.argv[1] == "cuda")
    if sys.argv[1] == "cuda":
        assert torch.cuda.is_available()
    x = torch.tensor(2.0, requires_grad=True)
    (x * x).backward()
    assert x.grad.item() == 4.0
except (ImportError, AssertionError, OSError):
    sys.exit(1)
PY
then
  echo "[science-env] Existing compatible environment; no changes"
  exit 0
fi
if [[ "$FROZEN_COUNT" != "0" ]]; then
  echo "ERROR: pinned frozen protocols refer to this environment; refusing pip install" >&2
  exit 3
fi
FREE_KIB="$(df -Pk "$ROOT/runtime" | awk 'END {print $4}')"
MIN_KIB=6291456
if [[ "$BACKEND" == "cuda" ]]; then MIN_KIB=10485760; fi
if [[ "$FREE_KIB" -lt "$MIN_KIB" ]]; then
  echo "ERROR: insufficient free disk for $BACKEND ($(df -h "$ROOT/runtime" | tail -1))" >&2
  exit 4
fi
if [[ ! -x "$PY" ]]; then /usr/bin/python3 -m venv "$ENV_ROOT"; fi
echo "[science-env] Installing Danus, pinned NumPy/SciPy and $BACKEND PyTorch"
"$PY" -m pip install --disable-pip-version-check --no-cache-dir -e "$ROOT[dev]"
"$PY" -m pip install --disable-pip-version-check --no-cache-dir "numpy==1.26.4" "scipy==1.14.1"
if [[ "$BACKEND" == "cuda" ]]; then
  "$PY" -m pip install --disable-pip-version-check --no-cache-dir \
    "torch==2.5.1+cu124" --index-url https://download.pytorch.org/whl/cu124
else
  "$PY" -m pip install --disable-pip-version-check --no-cache-dir \
    "torch==2.5.1+cpu" --index-url https://download.pytorch.org/whl/cpu
fi
"$PY" -m pip check
"$PY" - "$ENV_ROOT" "$BACKEND" <<'PY'
import json, platform, sys
from pathlib import Path
import numpy, scipy, torch
from danus.core.runtime import identity
root, backend = Path(sys.argv[1]), sys.argv[2]
assert numpy.__version__ == "1.26.4" and scipy.__version__ == "1.14.1"
assert torch.__version__.startswith("2.5.1")
if backend == "cuda":
    assert torch.version.cuda and torch.cuda.is_available()
    x = torch.tensor(2.0, device="cuda", requires_grad=True)
else:
    assert torch.version.cuda is None
    x = torch.tensor(2.0, requires_grad=True)
(x * x).backward()
assert x.grad.item() == 4.0
(root / "danus_runtime_identity.json").write_text(
    json.dumps(identity(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print("READY", platform.python_version(), numpy.__version__,
      scipy.__version__, torch.__version__, str(x.device),
      "gradient", x.grad.item())
PY
"$PY" -m pip freeze --all > "$ENV_ROOT/requirements.lock.txt"
echo "[science-env] READY; this venv is distinct from runtime/venv"
