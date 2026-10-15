#!/usr/bin/env python3
"""Resolve the specific 2026-10-11 INTERPRET-Evidence merge without dropping local code.

Run after fetching origin/fix/interpret-evidence-bound-read-20261011 while the
Git merge remains open. Uses the already-merged server.py (index stage 0), and
the two unmerged files at stages 2/3. Keeps the original locally-proven Evidence
getter as a legacy helper, routes the new scoped INTERPRET reader separately,
and retains all local tools/tests. No project runtime or scientific state touched.
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

BRANCH = "origin/fix/interpret-evidence-bound-read-20261011"
SERVER = "danus/gateway/server.py"
ROLES = "danus/gateway/roles.py"
TESTS = "danus/execution/tests/test_scientific_harness.py"
NEW_TESTS = "danus/gateway/tests/test_interpret_evidence_access.py"
TOOL = "experiment_evidence_get"


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], text=True, stderr=subprocess.PIPE,
    ).strip("\n")


def tree(s: str) -> ast.Module:
    return ast.parse(s)


def one_function(s: str, name: str) -> ast.FunctionDef:
    matches = [
        n for n in tree(s).body
        if isinstance(n, ast.FunctionDef) and n.name == name
    ]
    if len(matches) != 1 or matches[0].decorator_list:
        raise RuntimeError(f"expected one undecorated {name} function, found {len(matches)}")
    return matches[0]


def function_text(source: str, fn: ast.FunctionDef) -> str:
    return "".join(source.splitlines(keepends=True)[fn.lineno - 1:fn.end_lineno])


def changed_name(body: str, old: str, new: str) -> str:
    pat = rf"(?m)^def {re.escape(old)}\("
    if len(re.findall(pat, body)) != 1:
        raise RuntimeError(f"unexpected function header: {old}")
    return re.sub(pat, "def " + new + "(", body, count=1)


def assert_same_function(a: ast.FunctionDef, b: ast.FunctionDef) -> bool:
    return ast.dump(a, include_attributes=False) == ast.dump(b, include_attributes=False)


def rebuild_server() -> str:
    path = Path(SERVER)
    merged = path.read_text(encoding="utf-8")
    ours = git("show", "HEAD:" + SERVER)
    incoming = git("show", BRANCH + ":" + SERVER)
    old_fn = one_function(ours, TOOL)
    new_fn = one_function(incoming, TOOL)
    current = [
        n for n in tree(merged).body
        if isinstance(n, ast.FunctionDef) and n.name == TOOL
    ]
    if len(current) != 2:
        raise RuntimeError("expected precisely two auto-merged Evidence getters; nothing changed")
    if not any(assert_same_function(x, old_fn) for x in current):
        raise RuntimeError("the local getter was edited during auto-merge; refusing to drop it")
    if not any(assert_same_function(x, new_fn) for x in current):
        raise RuntimeError("incoming getter not preserved exactly; refusing guessed resolution")

    old = changed_name(function_text(ours, old_fn), TOOL, "_legacy_" + TOOL)
    new = changed_name(function_text(incoming, new_fn), TOOL, "_interpret_" + TOOL)
    wrapper = '''def experiment_evidence_get(
    evidence_id: str,
    artifact_index: Optional[int] = None,
    json_pointer: str = "",
) -> Dict[str, Any]:
    """Preserve legacy reads; restrict new raw data access to an INTERPRET handoff.

    An active Researcher INTERPRET route always uses the new exact-Evidence
    authorization; it can never fall back to the legacy brief on denial.
    """
    binding = _BOUND_WORKER
    interpret_route = False
    if binding is not None and binding.get("role") == "researcher":
        with capability.scope(binding):
            ledger = _pl()
            current = ledger.current(_author(), refresh_stale=False)
            if current and current.get("assignment_id") == binding["assignment_id"]:
                handoff_id = str(current.get("handoff_id") or "")
                route = HandoffRequests(ledger).get(handoff_id) if handoff_id else None
                interpret_route = bool(route and route.get("kind") == "INTERPRET")
    if interpret_route:
        return _interpret_experiment_evidence_get(
            evidence_id, artifact_index=artifact_index, json_pointer=json_pointer,
        )
    if artifact_index is not None or json_pointer:
        raise PermissionError("raw JSON inspection requires exact INTERPRET authorization")
    return _legacy_experiment_evidence_get(evidence_id)


'''
    replacement = old.rstrip() + "\n\n\n" + new.rstrip() + "\n\n\n" + wrapper
    lines = merged.splitlines(keepends=True)
    first = min(n.lineno for n in current)
    for n in sorted(current, key=lambda n: n.lineno, reverse=True):
        del lines[n.lineno - 1:n.end_lineno]
    lines.insert(first - 1, replacement)
    result = "".join(lines)
    if result.count('"experiment_evidence_get": experiment_evidence_get,') > 1:
        # Multiple identical dict keys have no meaning; preserve the sole entry.
        result = re.sub(
            r'(?m)^    "experiment_evidence_get": experiment_evidence_get,\n',
            "", result,
        )
        anchor = '    "experiment_protocol_get": experiment_protocol_get,'
        if result.count(anchor) != 1:
            raise RuntimeError("cannot place one Evidence tool registration")
        result = result.replace(anchor, anchor + '\n    "experiment_evidence_get": experiment_evidence_get,')
    if result.count('"experiment_evidence_get": experiment_evidence_get,') != 1:
        raise RuntimeError("exactly one Gateway registration is required")
    bound = result.partition("def _bound_tool(fn):")[2]
    if not bound or '"experiment_evidence_get"' not in bound.partition("def build_app(")[0]:
        raise RuntimeError("Gateway must avoid double-locking the self-scoped Evidence getter")
    resolved = tree(result)
    if len([n for n in resolved.body if isinstance(n, ast.FunctionDef)
            and n.name == TOOL]) != 1:
        raise RuntimeError("public Evidence getter not unique after merge")
    for name in ("_legacy_" + TOOL, "_interpret_" + TOOL):
        one_function(result, name)
    return result


def assignment(source: str, name: str) -> ast.AST:
    matches = [
        node for node in tree(source).body
        if ((isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
             and node.target.id == name)
            or (isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets
            )))
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one {name} declaration")
    return matches[0].value


def as_role_values(source: str) -> dict[str, list[str]]:
    mapping = assignment(source, "ROLE_TOOLS")
    if not isinstance(mapping, ast.Dict):
        raise RuntimeError("ROLE_TOOLS is not a static mapping")
    all_values = list(ast.literal_eval(assignment(source, "ALL_TOOLS")))
    out = {}
    for key, value in zip(mapping.keys, mapping.values):
        role = ast.literal_eval(key)
        out[role] = list(all_values) if isinstance(value, ast.Name) and value.id == "ALL_TOOLS" else list(ast.literal_eval(value))
    return out


def insert_in_tuple(source: str, node: ast.AST, value: str) -> tuple[int, int, str]:
    if not isinstance(node, (ast.Tuple, ast.List)):
        raise RuntimeError("tool declaration must be a static list/tuple")
    existing = list(ast.literal_eval(node))
    if value in existing:
        return (-1, -1, "")
    reference = "experiment_protocol_get"
    pos = existing.index(reference) + 1 if reference in existing else len(existing)
    existing.insert(pos, value)
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    start = offsets[node.lineno - 1] + node.col_offset
    end = offsets[node.end_lineno - 1] + node.end_col_offset
    replacement = "(" + ", ".join(repr(n) for n in existing) + ",)"
    return (start, end, replacement)


def rebuild_roles() -> tuple[str, dict[str, list[str]]]:
    ours = git("show", ":2:" + ROLES)
    theirs = git("show", ":3:" + ROLES)
    original = as_role_values(ours)
    proposed = as_role_values(theirs)
    novel = {name: set(proposed.get(name, [])) - set(original.get(name, []))
             for name in proposed}
    if any(extras - {TOOL} for extras in novel.values()):
        raise RuntimeError("incoming roles contain unexpected additions; refusing to discard them")
    if TOOL not in proposed.get("researcher", []):
        raise RuntimeError("incoming branch does not grant the Reader tool")

    roles_node = assignment(ours, "ROLE_TOOLS")
    all_node = assignment(ours, "ALL_TOOLS")
    assert isinstance(roles_node, ast.Dict)
    researcher = next(
        v for k, v in zip(roles_node.keys, roles_node.values)
        if ast.literal_eval(k) == "researcher"
    )
    edits = [insert_in_tuple(ours, all_node, TOOL), insert_in_tuple(ours, researcher, TOOL)]
    result = ours
    for start, end, replacement in sorted(edits, reverse=True):
        if start >= 0:
            result = result[:start] + replacement + result[end:]
    tree(result)
    final = as_role_values(result)
    if TOOL not in final["researcher"] or TOOL not in final["all"]:
        raise RuntimeError("tool missing from Researcher/ALL surface")
    if set(final["reviewer"]) != set(original["reviewer"]) or set(final["experimenter"]) != set(original["experimenter"]):
        raise RuntimeError("existing Reviewer/Experimenter permissions must be preserved")
    return result, final


def rebuild_test(roles: dict[str, list[str]]) -> str:
    ours = git("show", ":2:" + TESTS)
    theirs = git("show", ":3:" + TESTS)
    def tests(s: str) -> set[str]:
        return {n.name for n in tree(s).body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name.startswith("test_")}
    if not tests(theirs) <= tests(ours):
        raise RuntimeError("incoming branch contains tests absent locally; do not discard them")
    tree_ours = tree(ours)
    lines = ours.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    edits = []
    for node in ast.walk(tree_ours):
        if not isinstance(node, ast.Assert):
            continue
        expr = node.test
        if not isinstance(expr, ast.Compare) or len(expr.comparators) != 1:
            continue
        call = expr.left
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "tools_for" and len(call.args) == 1
                and isinstance(call.args[0], ast.Constant)):
            continue
        role = call.args[0].value
        if role not in {"researcher", "reviewer", "experimenter"} or not isinstance(expr.comparators[0], ast.List):
            continue
        rhs = expr.comparators[0]
        start = offsets[rhs.lineno - 1] + rhs.col_offset
        end = offsets[rhs.end_lineno - 1] + rhs.end_col_offset
        edits.append((start, end, repr(roles[role]), role))
    if {x[3] for x in edits} != {"researcher", "reviewer", "experimenter"}:
        raise RuntimeError("could not locate all three existing role snapshot assertions")
    for start, end, replacement, role in sorted(edits, reverse=True):
        ours = ours[:start] + replacement + ours[end:]
    tree(ours)
    return ours


def main() -> None:
    if not Path(".git").exists() or not Path(NEW_TESTS).is_file():
        raise RuntimeError("run in ~/danus-r with the incoming Evidence branch present")
    git("rev-parse", "--verify", "MERGE_HEAD")
    unresolved = set(git("diff", "--name-only", "--diff-filter=U").splitlines())
    if unresolved != {ROLES, TESTS}:
        raise RuntimeError(f"unexpected unmerged files: {sorted(unresolved)}")
    # All transformations and AST checks happen in memory before any write.
    out_server = rebuild_server()
    out_roles, permissions = rebuild_roles()
    out_tests = rebuild_test(permissions)
    # The remote branch received a follow-up test correction after the user
    # began this merge. Import that exact tested source too; do not leave a
    # stale test on disk from the original MERGE_HEAD commit.
    if subprocess.run(["git", "diff", "--quiet", "--", NEW_TESTS]).returncode:
        raise RuntimeError("local Evidence regression test has uncommitted edits")
    remote_test = git("show", BRANCH + ":" + NEW_TESTS)
    resolver_name = "scripts/resolve_interpret_evidence_merge_20261011.py"
    remote_resolver = git("show", BRANCH + ":" + resolver_name)
    outputs = {
        SERVER: out_server, ROLES: out_roles, TESTS: out_tests,
        NEW_TESTS: remote_test, resolver_name: remote_resolver,
    }
    for path, contents in outputs.items():
        tree(contents)
    # Local code is preserved in the new _legacy_ helper. No runtime data touched.
    for path, contents in outputs.items():
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(contents, encoding="utf-8")
    subprocess.check_call(["git", "add", *outputs])
    assert not git("diff", "--name-only", "--diff-filter=U")
    print("OK: original Evidence getter retained as _legacy_experiment_evidence_get")
    print("OK: new scoped INTERPRET Evidence lookup installed")
    print("OK: local role permissions and tests preserved; conflicts staged")
    print("NEXT: run pytest; only then git merge --continue")


if __name__ == "__main__":
    main()
