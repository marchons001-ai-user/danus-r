"""Content-addressed execution files, published before their protocol."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile


def relative_path(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise ValueError("bundle path must be a non-empty relative POSIX path")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in raw.split("/")):
        raise ValueError(f"bundle path must be canonical and relative: {raw}")
    return raw


def _identity(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _copy(root: Path, name: str, target: Path) -> dict:
    """Walk with openat/O_NOFOLLOW so a renamed parent cannot redirect the read."""
    parts = relative_path(name).split("/")
    root = Path(root).absolute()
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in (*root.parts[1:], *parts[:-1]):
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        source_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        import stat
        with os.fdopen(source_fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError(f"bundle source must be a regular file: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with target.open("xb") as output:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            return {"path": name, "sha256": digest.hexdigest(), "size": size}
    finally:
        os.close(fd)


def freeze(root: Path, source: Path, files: list[str], refs: list[dict]) -> str:
    if not isinstance(files, list) or not files:
        raise ValueError("execution_files must explicitly declare code and config files")
    names = {relative_path(name) for name in files}
    names.update(relative_path(ref["path"]) for ref in refs)
    parent = Path(root) / "evidence" / "bundles"
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".freeze-", dir=parent) as temporary:
        stage = Path(temporary) / "bundle"
        stage.mkdir()
        manifest = {"schema_version": 1, "files": [
            _copy(source, name, stage / "files" / name) for name in sorted(names)
        ]}
        by_name = {ref["path"]: ref for ref in manifest["files"]}
        for ref in refs:
            if any(ref.get(key) != by_name[ref["path"]][key] for key in ("sha256", "size")):
                raise ValueError(f"bundle source differs from declared reference: {ref['path']}")
        bundle_id = _identity(manifest)
        with (stage / "manifest.json").open("w") as output:
            json.dump(manifest, output, ensure_ascii=False, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        # Flush child directories before publishing the directory entry.
        for directory, _, _ in os.walk(stage, topdown=False):
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        target = parent / bundle_id
        try:
            os.rename(stage, target)
        except FileExistsError:
            with snapshot(root, bundle_id):
                pass
        except OSError:
            if not target.is_dir():
                raise
            with snapshot(root, bundle_id):
                pass
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return bundle_id


def load(root: Path, bundle_id: str) -> tuple[Path, dict]:
    if not isinstance(bundle_id, str) or not re.fullmatch(r"[0-9a-f]{64}", bundle_id):
        raise ValueError("invalid execution bundle_id")
    directory = Path(root) / "evidence" / "bundles" / bundle_id
    manifest = json.loads((directory / "manifest.json").read_text())
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or not isinstance(manifest.get("files"), list) or _identity(manifest) != bundle_id):
        raise ValueError("execution bundle manifest identity mismatch")
    names = [relative_path(ref["path"]) for ref in manifest["files"]]
    if names != sorted(set(names)) or not names:
        raise ValueError("execution bundle manifest paths are not unique and sorted")
    return directory / "files", manifest


@contextmanager
def snapshot(root: Path, bundle_id: str):
    source, manifest = load(root, bundle_id)
    base = Path.home() / ".cache" / "danus-bundle"
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="run-", dir=base) as temporary:
        target = Path(temporary)
        for ref in manifest["files"]:
            if _copy(source, ref["path"], target / ref["path"]) != ref:
                raise ValueError(f"frozen execution bundle file changed: {ref['path']}")
        yield target, manifest
