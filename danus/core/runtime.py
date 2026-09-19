"""Compact identity of the Python runtime and its installed dependency locks."""

import base64
import hashlib
from importlib import metadata
import json
from pathlib import Path
import platform
import sys
import sysconfig


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _file_digest(path, algorithm="sha256"):
    digest = hashlib.new(algorithm)
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def identity():
    # pyproject has unpinned extras. Installed wheel RECORDs are the actual lock;
    # verify their hashes, without publishing a second site-packages manifest.
    paths = sorted({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})
    dependencies = []
    for dist in metadata.distributions(path=paths):
        record = dist.read_text("RECORD")
        if record is None:
            raise ValueError("execution runtime dependency has no installed RECORD")
        for entry in dist.files or []:
            if entry.hash is None:
                continue
            actual = _file_digest(dist.locate_file(entry), entry.hash.mode)
            if base64.urlsafe_b64encode(actual).rstrip(b"=").decode() != entry.hash.value:
                raise ValueError(f"execution runtime dependency changed: {entry}")
        dependencies.append({
            "name": dist.metadata["Name"], "version": dist.version,
            "record": hashlib.sha256(record.encode()).hexdigest(),
            "metadata": hashlib.sha256((dist.read_text("METADATA") or "").encode()).hexdigest(),
            "direct_url": dist.read_text("direct_url.json"),
        })
    dependencies.sort(key=lambda item: json.dumps(item, sort_keys=True))
    executable = Path(sys.executable)
    config = Path(sys.prefix) / "pyvenv.cfg"
    return {
        "schema_version": 1,
        "implementation": platform.python_implementation(), "version": sys.version,
        "executable": str(executable.absolute()), "executable_target": str(executable.resolve()),
        "executable_sha256": _file_digest(executable).hex(),
        "prefix": str(Path(sys.prefix).resolve()), "base_prefix": str(Path(sys.base_prefix).resolve()),
        "venv_config_sha256": _file_digest(config).hex() if config.is_file() else None,
        "dependencies_sha256": _digest(dependencies),
    }


def validate(protocol):
    expected = protocol.get("execution_runtime")
    if not isinstance(expected, dict) or protocol.get("runtime_id") != _digest(expected):
        raise ValueError("runnable protocol requires a valid execution runtime identity")
    if identity() != expected:
        raise ValueError("execution runtime changed since protocol freeze")
