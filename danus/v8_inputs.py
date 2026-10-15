"""Operator-side, content-checked input staging for V8 scientific projects.

Proof scope: exact byte identity with named local source files at staging
time. Repository revision and extraction metadata are provenance clues, NOT
independent authentication of an upstream biological dataset.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_metadata(source_dir, extraction_script=None, extraction_summary=None):
    try:
        root = subprocess.check_output(
            ["git", "-C", str(source_dir), "rev-parse", "--show-toplevel"],
            text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
        commit = subprocess.check_output(
            ["git", "-C", str(source_dir), "rev-parse", "HEAD"],
            text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
    except (OSError, subprocess.SubprocessError):
        root, commit = "", ""
    references = {}
    for name, candidate in (("extraction_script", extraction_script),
                            ("extraction_summary", extraction_summary)):
        if candidate is None:
            continue
        path = Path(candidate).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError(f"{name} is not a file: {path}")
        references[name] = {"path": str(path), "sha256": sha256(path),
                            "bytes": path.stat().st_size}
    return {"git_root": root, "git_HEAD_at_staging": commit,
            "extraction_references": references}


def stage_inputs(store, source_dir, files, dataset_label,
                 *, extraction_script=None, extraction_summary=None):
    """Run only before the first Worker task; no role may self-certify inputs."""
    source_dir = Path(source_dir).expanduser().resolve(strict=True)
    if not source_dir.is_dir():
        raise ValueError("source_dir must be a directory")
    if not isinstance(dataset_label, str) or not dataset_label.strip():
        raise ValueError("dataset_label is required (operator-declared, not authenticated)")
    if not files or len(files) > 32 or len(set(files)) != len(files):
        raise ValueError("specify 1-32 unique input basenames")
    for name in files:
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name)
                or name == "PROVENANCE.json"):
            raise ValueError(f"invalid input basename: {name!r}")
    state = store.state()
    if state["tasks"] or state["done"] or state["halt"]:
        raise PermissionError("inputs must be staged before first task dispatch")
    if state.get("inputs") is not None:
        raise PermissionError("input provenance already frozen; create a new project")
    workspace = store.root / "workspace"
    dest = workspace / "input"
    if dest.exists() or dest.is_symlink():
        raise FileExistsError(f"input folder already exists; never overwrite staged inputs: {dest}")

    metadata = _source_metadata(source_dir, extraction_script, extraction_summary)
    workspace.mkdir(parents=True, exist_ok=True)
    # Build in a temporary sibling; atomically publish one complete input set.
    with tempfile.TemporaryDirectory(prefix=".input-stage-", dir=workspace) as temporary:
        temp = Path(temporary)
        records = []
        for name in files:
            source = (source_dir / name).resolve(strict=True)
            if not source.is_relative_to(source_dir) or not source.is_file():
                raise ValueError(f"source path escapes directory or is not a file: {name}")
            original_hash = sha256(source)
            target = temp / name
            with source.open("rb") as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            if sha256(target) != original_hash or sha256(source) != original_hash:
                raise RuntimeError(f"source changed during staging: {name}")
            records.append({"path": f"input/{name}", "sha256": original_hash,
                            "bytes": target.stat().st_size,
                            "source_path": str(source)})
        manifest = {
            "schema": "danus-v8-input-provenance-v1",
            "dataset_label": dataset_label.strip(),
            "source_directory": str(source_dir),
            "source": metadata,
            "files": records,
            "verified_scope": "byte-for-byte identity with the named local source files at staging",
            "not_verified": "Original upstream dataset authenticity, extraction correctness and biological interpretation",
        }
        manifest_file = temp / "PROVENANCE.json"
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2)
                                 + "\n", encoding="utf-8")
        manifest_sha = sha256(manifest_file)
        temp.rename(dest)
    # Immutable authoritative event binds the manifest contents and input hashes.
    store.append("INPUT_STAGE", manifest_path="input/PROVENANCE.json",
                 manifest_sha256=manifest_sha, dataset_label=manifest["dataset_label"],
                 files=records)
    store.publish()
    return {"dataset_label": manifest["dataset_label"], "files": records,
            "manifest": str(dest / "PROVENANCE.json"), "manifest_sha256": manifest_sha}


def verify_inputs(store, state=None):
    state = state if state is not None else store.state()
    inputs = state.get("inputs")
    if not inputs:
        return
    base = (store.root / "workspace").resolve()
    manifest = base / "input/PROVENANCE.json"
    if (not manifest.is_file() or manifest.is_symlink()
            or sha256(manifest) != inputs["manifest_sha256"]):
        raise PermissionError("staged input provenance manifest missing or modified")
    declared = json.loads(manifest.read_text(encoding="utf-8"))
    if (declared.get("schema") != "danus-v8-input-provenance-v1"
            or declared.get("dataset_label") != inputs["dataset_label"]
            or declared.get("files") != inputs["files"]):
        raise PermissionError("staged input provenance disagrees with authoritative event")
    for item in inputs["files"]:
        path = (base / item["path"]).resolve()
        if (not path.is_relative_to(base) or not path.is_file()
                or path.stat().st_size != item["bytes"]
                or sha256(path) != item["sha256"]):
            raise PermissionError(f"staged input modified or missing: {item['path']}")
