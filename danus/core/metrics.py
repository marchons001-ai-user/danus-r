"""Shared primary-metric recomputation from frozen reducer code.

Experiment Runtime and empirical Verification both call ``reduce_metrics``.  The
frozen protocol names one reducer file by path/SHA-256/size plus a function name;
there is deliberately no second verifier-side metric implementation.
"""
from __future__ import annotations

import hashlib
import importlib.util
import math
from numbers import Real
from pathlib import Path
from typing import Any, Dict, List


def _resolve(root: Path, raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else Path(root) / path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reduce_metrics(
    project_dir: Path,
    protocol: Dict[str, Any],
    artifacts: List[Dict[str, Any]],
) -> Dict[str, float]:
    """Run the protocol-pinned reducer and return exactly its primary metrics.

    Reducer contract::

        def reduce_metrics(protocol: dict, artifacts: list[pathlib.Path]) -> dict

    ``artifacts`` are the raw artifact references already recorded by the runtime.
    The reducer code itself is hash-pinned in ``protocol['metric_reducer']``.
    """
    root = Path(project_dir)
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

    module_name = f"danus_metric_reducer_{expected_hash[:16]}"
    spec = importlib.util.spec_from_file_location(module_name, reducer_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load metric reducer: {reducer_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reducer = getattr(module, function_name, None)
    if not callable(reducer):
        raise ValueError(f"metric reducer has no callable {function_name!r}")

    result = reducer(protocol, artifact_paths)
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
