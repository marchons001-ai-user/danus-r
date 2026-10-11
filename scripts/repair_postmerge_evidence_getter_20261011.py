#!/usr/bin/env python3
"""Fix a completed Git merge that retained two experiment_evidence_get definitions.

Only edits danus/gateway/server.py. The local implementation from the first
parent remains callable, while the incoming INTERPRET read is used for its
exactly-bound Evidence. Scientific state and data are untouched.

Requires a clean completed merge. Refuses unknown edits/duplicate definitions.
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

FILE = Path("danus/gateway/server.py")
NAME = "experiment_evidence_get"
LEGACY = "_legacy_experiment_evidence_get"
INTERPRET = "_interpret_experiment_evidence_get"


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).rstrip("\n")


def functions(source: str, name: str) -> list[ast.FunctionDef]:
    return [
        n for n in ast.parse(source).body
        if isinstance(n, ast.FunctionDef) and n.name == name
    ]


def fn_text(source: str, n: ast.FunctionDef) -> str:
    return "".join(source.splitlines(keepends=True)[n.lineno - 1:n.end_lineno])


def rename(source: str, old: str, new: str) -> str:
    old_header = f"def {old}("
    if source.count(old_header) != 1 or not source.startswith(old_header):
        raise RuntimeError(f"unexpected function header: {old}")
    return source.replace(old_header, f"def {new}(", 1)


def same(a: ast.FunctionDef, b: ast.FunctionDef) -> bool:
    return ast.dump(a, include_attributes=False) == ast.dump(b, include_attributes=False)


WRAPPER = '''def experiment_evidence_get(
    evidence_id: str,
    artifact_index: Optional[int] = None,
    json_pointer: str = "",
) -> Dict[str, Any]:
    """Keep existing lookup but add exact Handoff-scoped Researcher inspection.

    A current INTERPRET assignment never falls back to the legacy grant on
    denial. Other existing read-only callers retain their earlier contract.
    """
    binding = _BOUND_WORKER
    interpretation = False
    if binding is not None and binding.get("role") == "researcher":
        with capability.scope(binding):
            ledger = _pl()
            current = ledger.current(_author(), refresh_stale=False)
            if current and current.get("assignment_id") == binding["assignment_id"]:
                hid = str(current.get("handoff_id") or "")
                handoff = HandoffRequests(ledger).get(hid) if hid else None
                interpretation = bool(handoff and handoff.get("kind") == "INTERPRET")
    if interpretation:
        return _interpret_experiment_evidence_get(
            evidence_id, artifact_index=artifact_index, json_pointer=json_pointer,
        )
    if artifact_index is not None or json_pointer:
        raise PermissionError("nested raw Evidence access requires bound INTERPRET")
    return _legacy_experiment_evidence_get(evidence_id)


'''


def main() -> None:
    if not FILE.is_file() or git("rev-parse", "--show-toplevel") != str(Path.cwd().resolve()):
        raise RuntimeError("run from the Danus-R repository root")
    if git("status", "--porcelain"):
        raise RuntimeError("working tree must be clean; do not overwrite pending edits")
    if subprocess.run(["git", "rev-parse", "--verify", "-q", "MERGE_HEAD"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        raise RuntimeError("merge must be completed first")
    current = FILE.read_text(encoding="utf-8")
    found = functions(current, NAME)
    if len(found) == 1:
        print("Already exactly one experiment_evidence_get: no repair needed")
        return
    if len(found) != 2:
        raise RuntimeError(f"expected exactly two merged getters; found {len(found)}")
    if functions(current, LEGACY) or functions(current, INTERPRET):
        raise RuntimeError("previous repair partially present")
    parent = git("show", "HEAD^1:" + str(FILE))
    incoming = git("show", "HEAD^2:" + str(FILE))
    old = functions(parent, NAME)
    new = functions(incoming, NAME)
    if len(old) != 1 or len(new) != 1 or old[0].decorator_list or new[0].decorator_list:
        raise RuntimeError("unknown parent Evidence getter shape")
    if sum(same(n, old[0]) for n in found) != 1 or sum(same(n, new[0]) for n in found) != 1:
        raise RuntimeError("merged getters are not the exact two parent definitions")
    # The old and incoming functions may appear in either order after auto-merge.
    start_line = min(n.lineno for n in found)
    lines = current.splitlines(keepends=True)
    for n in sorted(found, key=lambda n: n.lineno, reverse=True):
        del lines[n.lineno - 1:n.end_lineno]
    replaced = (
        rename(fn_text(parent, old[0]), NAME, LEGACY).rstrip()
        + "\n\n\n"
        + rename(fn_text(incoming, new[0]), NAME, INTERPRET).rstrip()
        + "\n\n\n"
        + WRAPPER
    )
    lines.insert(start_line - 1, replaced)
    updated = "".join(lines)
    # Remove exact duplicate registration keys; do not modify other Gateway tools.
    key = '    "experiment_evidence_get": experiment_evidence_get,'
    occurrences = [m for m in re.finditer(re.escape(key), updated)]
    if len(occurrences) not in (1, 2):
        raise RuntimeError("unknown Evidence tool registration shape")
    if len(occurrences) == 2:
        pos = occurrences[1].start()
        updated = updated[:pos] + updated[pos:].replace(key + "\n", "", 1)
    if updated.count(key) != 1:
        raise RuntimeError("unable to preserve single Evidence registration")
    if len(functions(updated, NAME)) != 1:
        raise RuntimeError("public getter is not unique")
    for name in (LEGACY, INTERPRET):
        if len(functions(updated, name)) != 1:
            raise RuntimeError(f"missing preserved implementation {name}")
    if '"experiment_evidence_get"' not in updated.partition("def _bound_tool(fn):")[2].partition("def build_app(")[0]:
        raise RuntimeError("self-scoped getter requires no outer duplicate Gateway lock")
    ast.parse(updated)
    FILE.write_text(updated, encoding="utf-8")
    print("FIXED: one public Evidence getter, both original implementations retained")
    print("FIXED: INTERPRET read requires exact current Handoff authority")
    print("NEXT: run targeted tests, then commit this source-only repair")


if __name__ == "__main__":
    main()
