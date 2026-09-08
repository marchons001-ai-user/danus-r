"""Shared primary-metric recomputation from frozen reducer code.

Experiment and verifier use the same isolated runner, Python environment and
numeric normalization. The protocol pins reducer bytes before snapshot execution.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import shutil
import sys
import tempfile
from numbers import Real
from pathlib import Path
from typing import Any, Dict, List

from danus.sandbox import run_sandboxed


REDUCER_TIMEOUT_SECONDS = 30.0
_RESULT_MARKER = "DANUS_REDUCER_RESULT="
_RUNNER_SOURCE = r'''from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import math
import sys
from numbers import Real
from pathlib import Path


MARKER = "DANUS_REDUCER_RESULT="

# NORMALIZE_METRICS

def main() -> int:
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    reducer_path = Path(request["reducer_path"])
    spec = importlib.util.spec_from_file_location("danus_sandboxed_reducer", reducer_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load metric reducer: {reducer_path}")
    module = importlib.util.module_from_spec(spec)
    function_name = request["function"]
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        spec.loader.exec_module(module)
        reducer = getattr(module, function_name, None)
        if not callable(reducer):
            raise RuntimeError(f"metric reducer has no callable {function_name!r}")
        result = reducer(
            request["protocol"],
            [Path(path) for path in request["artifacts"]],
        )
    result = _normalize_metrics(request["protocol"], result)
    encoded = json.dumps(
        result,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    json.loads(encoded)
    print(MARKER + encoded)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
'''


def _resolve(root: Path, raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else Path(root) / path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_reducer_inputs(
    root: Path,
    protocol: Dict[str, Any],
    artifacts: List[Dict[str, Any]],
) -> tuple[Path, str, List[Path]]:
    ref = protocol.get("metric_reducer")
    if not isinstance(ref, dict):
        raise ValueError("frozen protocol has no metric_reducer")

    raw_path = str(ref.get("path") or "")
    expected_hash = str(ref.get("sha256") or "")
    expected_size = ref.get("size")
    function_name = str(ref.get("function") or "reduce_metrics")
    if not raw_path or not expected_hash or not isinstance(expected_size, int):
        raise ValueError("metric_reducer requires path, sha256, and integer size")
    if not function_name:
        raise ValueError("metric_reducer function is empty")

    if protocol.get("bundle_id"):
        from . import bundle
        bundle_root, manifest = bundle.load(root, protocol["bundle_id"])
        name = bundle.relative_path(raw_path)
        if not any(item == {"path": name, "sha256": expected_hash, "size": expected_size}
                   for item in manifest["files"]):
            raise ValueError("metric reducer is not bound by execution bundle")
        reducer_path = bundle_root / name
    else:
        reducer_path = _resolve(root, raw_path)
    if not reducer_path.is_file():
        raise ValueError(f"metric reducer missing: {reducer_path}")
    if reducer_path.stat().st_size != expected_size:
        raise ValueError("metric reducer size changed since protocol freeze")
    if _sha256(reducer_path) != expected_hash:
        raise ValueError("metric reducer SHA-256 changed since protocol freeze")

    artifact_paths: List[Path] = []
    for ref_item in artifacts:
        if not isinstance(ref_item, dict) or not str(ref_item.get("path") or ""):
            raise ValueError("artifact reference requires path")
        path = _resolve(root, str(ref_item["path"]))
        if not path.is_file():
            raise ValueError(f"raw artifact missing: {path}")
        artifact_paths.append(path)
    return reducer_path, function_name, artifact_paths


def _normalize_metrics(protocol: Dict[str, Any], result: Any) -> Dict[str, float]:
    if not isinstance(result, dict):
        raise ValueError("metric reducer must return a dict")

    expected_names = [str(x) for x in protocol.get("primary_metrics", [])]
    if set(result) != set(expected_names):
        raise ValueError(
            f"metric reducer keys {sorted(result)} do not match frozen primary_metrics "
            f"{sorted(expected_names)}"
        )

    normalized: Dict[str, float] = {}
    for name in expected_names:
        value = result[name]
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError(f"primary metric {name!r} must be numeric")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"primary metric {name!r} must be finite")
        normalized[name] = number
    return normalized


def _copy_snapshot(source: Path, target: Path, ref: Dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    expected_size = ref.get("size")
    if isinstance(expected_size, int) and target.stat().st_size != expected_size:
        raise ValueError(f"snapshot size changed while copying: {source}")
    expected_hash = str(ref.get("sha256") or "")
    if expected_hash and _sha256(target) != expected_hash:
        raise ValueError(f"snapshot SHA-256 changed while copying: {source}")


def _sandbox_tmp_root() -> Path:
    root = Path.home() / ".cache" / "danus-reducer"
    root.mkdir(parents=True, exist_ok=True)
    return root


def reduce_metrics_sandboxed(
    project_dir: Path,
    protocol: Dict[str, Any],
    artifacts: List[Dict[str, Any]],
) -> Dict[str, float]:
    """Recompute primary metrics without executing project code in this process."""
    root = Path(project_dir).resolve()
    reducer_path, function_name, artifact_paths = _validated_reducer_inputs(
        root, protocol, artifacts
    )
    reducer_ref = protocol["metric_reducer"]
    tmp_root = _sandbox_tmp_root()
    with tempfile.TemporaryDirectory(prefix="input-", dir=str(tmp_root)) as input_dir_text:
        input_dir = Path(input_dir_text)
        staged_reducer = input_dir / "reducer.py"
        _copy_snapshot(reducer_path, staged_reducer, reducer_ref)
        staged_artifacts: List[Path] = []
        for index, (source, ref_item) in enumerate(zip(artifact_paths, artifacts)):
            if not isinstance(ref_item, dict):
                raise ValueError("artifact reference must be an object")
            target = input_dir / "artifacts" / f"{index}-{source.name}"
            _copy_snapshot(source, target, ref_item)
            staged_artifacts.append(target)

        with tempfile.TemporaryDirectory(prefix="result-", dir=str(tmp_root)) as result_dir_text:
            result_dir = Path(result_dir_text)
            request_path = result_dir / "request.json"
            runner_path = result_dir / "runner.py"
            request_path.write_text(
                json.dumps(
                    {
                        "protocol": protocol,
                        "reducer_path": str(staged_reducer),
                        "function": function_name,
                        "artifacts": [str(path) for path in staged_artifacts],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            runner_source = _RUNNER_SOURCE.replace("# NORMALIZE_METRICS", inspect.getsource(_normalize_metrics))
            compile(runner_source, str(runner_path), "exec")
            runner_path.write_text(runner_source, encoding="utf-8")
            completed = run_sandboxed(
                [sys.executable, "-I", str(runner_path), str(request_path)],
                cwd=result_dir,
                readable_paths=[input_dir, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()],
                writable_paths=[result_dir],
                network=False,
                env={},
                timeout=REDUCER_TIMEOUT_SECONDS,
            )

    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()
        suffix = f": {detail[-500:]}" if detail else ""
        raise ValueError(
            f"sandboxed metric reducer failed with exit code {completed.returncode}{suffix}"
        )
    lines = [line for line in (completed.stdout or "").splitlines() if line]
    if len(lines) != 1 or not lines[0].startswith(_RESULT_MARKER):
        raise ValueError("sandboxed metric reducer returned non-JSON output")
    try:
        result = json.loads(lines[0][len(_RESULT_MARKER):])
    except json.JSONDecodeError as exc:
        raise ValueError("sandboxed metric reducer returned invalid JSON") from exc
    return _normalize_metrics(protocol, result)


def reduce_metrics(
    project_dir: Path,
    protocol: Dict[str, Any],
    artifacts: List[Dict[str, Any]],
) -> Dict[str, float]:
    """Recompute protocol metrics through the official Codex sandbox."""
    return reduce_metrics_sandboxed(project_dir, protocol, artifacts)


def metrics_equal(recorded: Any, recomputed: Dict[str, float]) -> bool:
    """Tight numeric equality for a verifier rerun of the exact same reducer."""
    if not isinstance(recorded, dict) or set(recorded) != set(recomputed):
        return False
    for name, expected in recomputed.items():
        value = recorded.get(name)
        if isinstance(value, bool) or not isinstance(value, Real):
            return False
        if not math.isclose(float(value), expected, rel_tol=1e-12, abs_tol=1e-12):
            return False
    return True
