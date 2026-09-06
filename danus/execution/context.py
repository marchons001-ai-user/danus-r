"""Small, file-first Agent checkpoint and context-budget helpers.

Scientific worker rounds remain ordinary subprocesses. This module only extracts
structured telemetry when a backend provides it, writes bounded recoverable state,
and validates model-produced migration data. It has no scheduler, database, or
LLM call of its own.

Codex ``turn.completed`` reports cumulative turn usage, not exact context-window
occupancy. The legacy ``tokens`` field therefore remains total input+output usage
for compatibility, while ``uncached_tokens`` is the smaller input-minus-cache plus
output signal used by the context token guard when the structured fields exist.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from danus.core import ProgressLedger
from danus.core._util import utc_now

CHECKPOINT_NAME = "agent_checkpoint.json"
MIGRATION_NAME = "context_migration.json"
ROLLOVER_NAME = ".context_rollover.json"

_DEFAULT_TOKENS = 60_000
_DEFAULT_TOOL_CALLS = 64
_DEFAULT_COMPACTIONS = 2
_DEFAULT_SECONDS = 1_800

_TOOL_ITEM_TYPES = {"command_execution", "mcp_tool_call", "web_search"}
_TOKEN_LINE_RE = re.compile(r"(?im)^tokens used\s+([0-9][0-9,]*)\s*$")
_REVIEW_CLASS_PREFIX = "DANUS_REVIEW_CLASS:"
_VERIFIED_REVIEW_CLASS_PREFIX = "DANUS_REVIEW_CLASS_VERIFIED:"
_REVIEW_CLASSES = {"SCIENTIFIC_AMBIGUITY", "DETERMINISTIC"}
_MIGRATION_FIELDS = (
    "current_objective",
    "established_results",
    "evidence_refs",
    "fact_refs",
    "decisions_already_made",
    "failed_paths",
    "open_questions",
    "next_action",
    "files_that_matter",
)
_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def _empty_telemetry() -> Dict[str, Any]:
    return {
        "structured": False,
        "thread_id": None,
        "tokens": 0,
        "uncached_tokens": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "tool_calls": 0,
        "compactions": 0,
        "last_agent_message": "",
    }


@dataclass
class LogCursor:
    """Ephemeral cursor for one growing round log; never persisted."""

    offset: int = 0
    remainder: bytes = b""
    tools: set[str] = field(default_factory=set)
    compactions: set[str] = field(default_factory=set)
    telemetry: Dict[str, Any] = field(default_factory=_empty_telemetry)
    structured_usage: bool = False
    raw_tail: str = ""

    def reset(self) -> None:
        self.offset = 0
        self.remainder = b""
        self.tools.clear()
        self.compactions.clear()
        self.telemetry = _empty_telemetry()
        self.structured_usage = False
        self.raw_tail = ""


def _positive_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(0, value)


def limits() -> Dict[str, int]:
    return {
        "tokens": _positive_env("DANUS_CONTEXT_MAX_TOKENS", _DEFAULT_TOKENS),
        "tool_calls": _positive_env("DANUS_CONTEXT_MAX_TOOL_CALLS", _DEFAULT_TOOL_CALLS),
        "compactions": _positive_env("DANUS_CONTEXT_MAX_COMPACTIONS", _DEFAULT_COMPACTIONS),
        "seconds": _positive_env("DANUS_CONTEXT_MAX_SECONDS", _DEFAULT_SECONDS),
    }


def _bounded_agent_message(message: str) -> str:
    markers = []
    kept = []
    for line in message.splitlines():
        stripped = line.strip()
        if stripped.startswith(_REVIEW_CLASS_PREFIX):
            markers.append(stripped[len(_REVIEW_CLASS_PREFIX):].strip())
            continue
        if stripped.startswith(_VERIFIED_REVIEW_CLASS_PREFIX):
            continue
        kept.append(line)
    text = "\n".join(kept)
    verified = markers[0] if len(markers) == 1 and markers[0] in _REVIEW_CLASSES else ""
    suffix = f"\n{_VERIFIED_REVIEW_CLASS_PREFIX} {verified}" if verified else ""
    return text[-max(0, 8000 - len(suffix)):] + suffix


def _consume_line(cursor: LogCursor, line: str) -> None:
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        match = _TOKEN_LINE_RE.search(line)
        if match and not cursor.structured_usage:
            try:
                value = int(match.group(1).replace(",", ""))
            except ValueError:
                return
            cursor.telemetry["tokens"] = value
            cursor.telemetry["uncached_tokens"] = value
        return
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        return

    out = cursor.telemetry
    out["structured"] = True
    etype = event["type"]
    if etype == "thread.started" and isinstance(event.get("thread_id"), str):
        out["thread_id"] = event["thread_id"]
    elif etype == "turn.completed":
        usage = event.get("usage")
        if isinstance(usage, dict):
            inp = usage.get("input_tokens")
            output = usage.get("output_tokens")
            if isinstance(inp, int) and isinstance(output, int):
                cursor.structured_usage = True
                for key in _USAGE_FIELDS:
                    value = usage.get(key)
                    if isinstance(value, int):
                        out[key] = max(out[key], max(0, value))
                out["tokens"] = max(out["tokens"], max(0, inp) + max(0, output))
    elif etype.startswith("item."):
        item = event.get("item")
        if not isinstance(item, dict):
            return
        item_type = str(item.get("type") or "")
        item_id = str(item.get("id") or f"{item_type}:{len(cursor.tools) + len(cursor.compactions)}")
        if item_type in _TOOL_ITEM_TYPES:
            cursor.tools.add(item_id)
        if "compaction" in item_type.lower():
            cursor.compactions.add(item_id)
        if etype == "item.completed" and item_type == "agent_message":
            msg = item.get("text")
            if isinstance(msg, str):
                out["last_agent_message"] = _bounded_agent_message(msg)


def inspect_log(path: Path, cursor: Optional[LogCursor] = None) -> Dict[str, Any]:
    """Extract Codex JSONL telemetry; optionally consume only newly appended bytes.

    Callers that omit ``cursor`` retain the historical one-shot behavior. A live
    round may reuse one cursor so polling cost is proportional to new log output,
    not the whole growing file.
    """
    own_cursor = cursor is None
    cur = cursor or LogCursor()
    p = Path(path)
    try:
        size = p.stat().st_size
        if size < cur.offset:
            cur.reset()
        with p.open("rb") as handle:
            handle.seek(cur.offset)
            data = handle.read()
            cur.offset = handle.tell()
    except OSError:
        return dict(cur.telemetry)

    if data:
        cur.raw_tail = (cur.raw_tail + data.decode("utf-8", errors="replace"))[-4000:]
    merged = cur.remainder + data
    lines = merged.split(b"\n")
    cur.remainder = lines.pop() if lines else b""
    for raw in lines:
        _consume_line(cur, raw.decode("utf-8", errors="replace").rstrip("\r"))

    # A one-shot read is over a stable file, so parse a final non-newline-terminated
    # record as well. Live cursors keep it until the writer completes the line.
    if own_cursor and cur.remainder:
        _consume_line(cur, cur.remainder.decode("utf-8", errors="replace").rstrip("\r"))
        cur.remainder = b""

    out = cur.telemetry
    out["tool_calls"] = len(cur.tools)
    out["compactions"] = len(cur.compactions)
    if cur.structured_usage:
        uncached_input = max(0, out["input_tokens"] - out["cached_input_tokens"])
        out["uncached_tokens"] = uncached_input + out["output_tokens"]
    result = dict(out)
    if not result["last_agent_message"] and cur.raw_tail:
        result["last_agent_message"] = cur.raw_tail
    return result


def budget_reason(telemetry: Dict[str, Any], elapsed_s: float) -> Optional[str]:
    cap = limits()
    total_tokens = int(telemetry.get("tokens") or 0)
    if "uncached_tokens" in telemetry:
        token_value = min(total_tokens, int(telemetry.get("uncached_tokens") or 0))
    else:
        token_value = total_tokens
    values = {
        "tokens": max(0, token_value),
        "tool_calls": int(telemetry.get("tool_calls") or 0),
        "compactions": int(telemetry.get("compactions") or 0),
        "seconds": int(max(0.0, elapsed_s)),
    }
    for key in ("compactions", "tokens", "tool_calls", "seconds"):
        if cap[key] and values[key] >= cap[key]:
            return f"{key}:{values[key]}/{cap[key]}"
    return None


def _path(worker_dir: Path, name: str) -> Path:
    return Path(worker_dir) / "local_memory" / name


def _task_hash(worker_dir: Path) -> str:
    try:
        text = (Path(worker_dir) / "TASK.md").read_text(encoding="utf-8")
    except OSError:
        text = ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _assignment(worker_dir: Path) -> Dict[str, Any]:
    wdir = Path(worker_dir)
    try:
        current = ProgressLedger(wdir.parents[1]).current(wdir.name, refresh_stale=True)
    except (FileNotFoundError, ValueError):
        current = None
    return current or {}


def write_checkpoint(
    worker_dir: Path,
    *,
    role: str,
    round_no: int,
    telemetry: Dict[str, Any],
    trigger: Optional[str] = None,
    migration: Optional[Dict[str, Any]] = None,
    assignment: Optional[Dict[str, Any]] = None,
) -> Path:
    wdir = Path(worker_dir)
    bound = dict(assignment) if assignment is not None else _assignment(wdir)
    task_hash = str(bound.get("task_sha256") or "") or _task_hash(wdir)
    telemetry_out = {
        "tokens": int(telemetry.get("tokens") or 0),
        "uncached_tokens": int(telemetry.get("uncached_tokens") or 0),
        "tool_calls": int(telemetry.get("tool_calls") or 0),
        "compactions": int(telemetry.get("compactions") or 0),
        "structured": bool(telemetry.get("structured")),
    }
    for key in _USAGE_FIELDS:
        telemetry_out[key] = int(telemetry.get(key) or 0)
    payload: Dict[str, Any] = {
        "schema_version": 1,
        "worker": wdir.name,
        "role": role,
        "task_sha256": task_hash,
        "assignment_id": bound.get("assignment_id"),
        "plan_node_id": bound.get("node_id"),
        "plan_version": bound.get("plan_version"),
        "round": int(round_no),
        "created_at": utc_now(),
        "trigger": trigger,
        "telemetry": telemetry_out,
        "last_agent_message": str(telemetry.get("last_agent_message") or "")[-8000:],
        "migration": migration,
    }
    path = _path(wdir, CHECKPOINT_NAME)
    _write_json(path, payload)
    return path


def checkpoint_for_prompt(
    worker_dir: Path,
    role: str,
    assignment: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    wdir = Path(worker_dir)
    path = _path(wdir, CHECKPOINT_NAME)
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("schema_version") != 1 or obj.get("worker") != wdir.name:
        return None
    if obj.get("role") != role:
        return None

    bound = dict(assignment) if assignment is not None else _assignment(wdir)
    if bound:
        if obj.get("assignment_id") != bound.get("assignment_id"):
            return None
        task_hash = str(bound.get("task_sha256") or "")
        if task_hash and obj.get("task_sha256") != task_hash:
            return None
    if assignment is None and obj.get("task_sha256") != _task_hash(wdir):
        return None
    if len(json.dumps(obj, ensure_ascii=False)) > 20_000:
        return None
    return obj


def write_rollover_request(worker_dir: Path, reason: str, telemetry: Dict[str, Any]) -> None:
    _write_json(
        _path(worker_dir, ROLLOVER_NAME),
        {"reason": reason, "telemetry": telemetry, "created_at": utc_now()},
    )


def pop_rollover_request(worker_dir: Path) -> Optional[Dict[str, Any]]:
    path = _path(worker_dir, ROLLOVER_NAME)
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        path.unlink(missing_ok=True)
        return None
    path.unlink(missing_ok=True)
    return obj if isinstance(obj, dict) else None


def migration_path(worker_dir: Path) -> Path:
    return _path(worker_dir, MIGRATION_NAME)


def load_migration(worker_dir: Path) -> Optional[Dict[str, Any]]:
    path = migration_path(worker_dir)
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    out: Dict[str, Any] = {}
    for key in _MIGRATION_FIELDS:
        value = obj.get(key)
        if key in ("current_objective", "next_action"):
            if not isinstance(value, str) or not value.strip():
                return None
            out[key] = value.strip()[:2000]
        else:
            if not isinstance(value, list):
                return None
            out[key] = [str(x)[:1000] for x in value[:20] if str(x).strip()]
    return out


def migration_prompt(worker_dir: Path, reason: str, prior_log: Path) -> str:
    rel_log = os.path.relpath(prior_log, Path(worker_dir))
    fields = ", ".join(_MIGRATION_FIELDS)
    return (
        "This is a Danus-R context migration turn, not scientific work. Do not continue "
        "the research, run experiments, browse externally, or edit project source code. "
        "Read TASK.md and the existing local_memory/agent_checkpoint.json first. Inspect "
        f"only the minimum shared Plan/Fact/Evidence state needed and, if necessary, at "
        f"most the tail of {rel_log}. The previous session was rolled over because "
        f"{reason}.\n\nWrite exactly one JSON object to "
        "local_memory/context_migration.json with these keys: " + fields + ". "
        "current_objective and next_action must be concise strings; every other field "
        "must be a short JSON array of strings. Preserve negative results, blockers, "
        "evidence/fact ids, decisions already made, and only files that matter. Do not "
        "include hidden reasoning or chain-of-thought. After writing the file, stop."
    )
