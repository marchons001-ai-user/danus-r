"""Shared fixtures for the danus.human_summary offline tests.

The read-only source fixture is the shipped example project
``.claude/skills/human-summary/examples/odd-sum/`` (it has ``fact_graph/facts/``
+ ``PROBLEM.md``). Anything that WRITES copies it to a tempdir first.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = _REPO_ROOT / "agents" / "skills" / "human-summary"                 # codex-facing writer prompt
MAIN_SKILL_DIR = _REPO_ROOT / ".claude" / "skills" / "human-summary"          # main-agent side (SKILL.md / render pipeline / examples)
EXAMPLE_PROJECT = MAIN_SKILL_DIR / "examples" / "odd-sum"


@contextlib.contextmanager
def temp_project():
    """Copy the example project to a tempdir; yield its path."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="human-summary-test-") as d:
        dst = Path(d) / "project"
        shutil.copytree(EXAMPLE_PROJECT, dst)
        from danus.core import FactGraph
        from danus.core.factgraph import parse_frontmatter
        graph = FactGraph(dst)
        pending = {p.stem: p.read_text() for p in graph.facts_dir.glob("*.md")}
        ids = {}
        while pending:
            for old, raw in list(pending.items()):
                meta = parse_frontmatter(raw)
                if any(p not in ids for p in meta["predecessors"]):
                    continue
                body = raw.split("\n## statement\n", 1)[1]
                statement, rest = body.split("\n## proof\n", 1)
                proof, intuition = rest.split("\n## intuition\n", 1)
                ids[old] = graph.add(problem_id="odd-sum", author="example-worker", statement=statement,
                                     proof=proof, intuition=intuition, predecessors=[ids[p] for p in meta["predecessors"]])
                (graph.facts_dir / f"{old}.md").unlink()
                del pending[old]
        yield dst


@contextlib.contextmanager
def env(**kv):
    """Temporarily set env vars (None deletes), restore after."""
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
