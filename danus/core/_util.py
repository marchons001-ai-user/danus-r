"""Small shared helpers: UTC timestamps and append-only JSONL I/O.

Append-only JSONL is the memory mechanism used across all three core stores.
"""

from __future__ import annotations

import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    """Append one intact JSONL record across concurrent writers/process crashes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    with path.open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.seek(0, 2)
            end = handle.tell()
            if end:
                handle.seek(-1, 2)
                if handle.read(1) != b"\n":
                    # A killed writer may leave one torn final record.  Preserve it
                    # for audit, but never concatenate the next valid event onto it.
                    handle.seek(0, 2)
                    handle.write(b"\n")
            handle.seek(0, 2)
            handle.write(data)
            handle.flush()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def iter_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("rb") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                yield payload


def read_jsonl(path: Path) -> list:
    return list(iter_jsonl(path))
