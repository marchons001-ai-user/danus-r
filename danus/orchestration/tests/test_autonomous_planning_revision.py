"""A2-8c bootstrap provenance and post-commit revision regressions. No LLM/network."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.execution import layout as L
from danus.orchestration import cli, planning


@contextmanager
def _root(tmp_path: Path):
    old = os.environ.get("DANUS_AGENTS_ROOT")
    os.environ["DANUS_AGENTS_ROOT"] = str(tmp_path / "projects")
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("DANUS_AGENTS_ROOT", None)
        else:
            os.environ["DANUS_AGENTS_ROOT"] = old


def _finish(project: str, worker: str) -> None:
    ProgressLedger(L.project_dir(project)).update(
        worker=worker,
        state="DONE",
        actor="runtime",
        outcome="COMPLETE",
    )


def _write_proposal(pdir: Path, payload: dict) -> None:
    path = planning._proposal_path(pdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**payload, "assignment_id": ProgressLedger(pdir).current("low")["assignment_id"]}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _approve_pending(pdir: Path, reviewer: str = "low2") -> str:
    graph = ResearchPlanGraph(pdir)
    change = graph.pending_changes()[0]
    graph.review_change(change["change_id"], reviewer=reviewer, approve=True, rationale="fixture")
    _finish("P", reviewer)
    return change["change_id"]


def test_bootstrap_provenance_survives_prompt_upgrade(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        first = planning.initialize_research("P", brief="Study one bounded question.")
        pdir = L.project_dir("P")
        first_id = first["bootstrap_assignment_id"]
        assert first_id
        record_path = pdir / planning._BOOTSTRAP_REL
        assert record_path.is_file()
        assert len(ProgressLedger(pdir).assignments()) == 1

        # Simulate an old checkout that ran bootstrap before durable provenance existed,
        # then upgrade Danus so the bootstrap prompt text/hash changes.
        record_path.unlink()
        monkeypatch.setattr(
            planning,
            "_bootstrap_task",
            lambda pdir, plan_version: "# Autonomous Research Bootstrap\n\nUPGRADED PROMPT\n",
        )
        resumed = planning.initialize_research("P")
        assert resumed["bootstrap_start"] == "existing"
        assert resumed["bootstrap_assignment_id"] == first_id
        assert len(ProgressLedger(pdir).assignments()) == 1
        recovered = json.loads(record_path.read_text(encoding="utf-8"))
        assert recovered["assignment_id"] == first_id
        assert recovered["recovered"] is True

        # Once provenance exists, further prompt changes are irrelevant to identity.
        monkeypatch.setattr(
            planning,
            "_bootstrap_task",
            lambda pdir, plan_version: "# Autonomous Research Bootstrap\n\nANOTHER UPGRADE\n",
        )
        again = planning.initialize_research("P")
        assert again["bootstrap_start"] == "existing"
        assert again["bootstrap_assignment_id"] == first_id
        assert len(ProgressLedger(pdir).assignments()) == 1


def test_post_commit_revision_advances_v2_to_v3(tmp_path: Path, monkeypatch):
    with _root(tmp_path):
        monkeypatch.setattr(cli, "_start_one", lambda wl: "started")
        init = planning.initialize_research("P", brief="Start with one research step and revise only if needed.")
        pdir = L.project_dir("P")
        _finish("P", "low")

        _write_proposal(pdir, {
            "base_plan_version": 1,
            "reason": "initial minimal route",
            "post_hoc": False,
            "evidence_refs": [],
            "operations": [{
                "op": "add_node",
                "node_id": "R1",
                "title": "Initial research",
                "summary": "Resolve the first bounded research question.",
                "parent": "P0",
                "depends_on": [],
            }],
            "tasks": [{
                "node_id": "R1",
                "role": "researcher",
                "task": "Resolve the first bounded question. If the route is structurally incomplete, propose the smallest revision.",
            }],
        })
        first_actions = planning.planning_step("P")
        assert any(x["action"] == "PLAN_PROPOSAL" and x["result"] == "captured" for x in first_actions)
        _approve_pending(pdir)
        commit_v2 = planning.planning_step("P")
        assert any(x["action"] == "PLAN_COMMIT" and x.get("plan_version") == 2 for x in commit_v2)

        r1_task = pdir / "tasks" / "v2" / "researcher" / "R1.md"
        text = r1_task.read_text(encoding="utf-8")
        assert "<current plan_version from the committed Research Plan>" in text
        assert "copy its integer `plan_version` exactly" in text
        assert '"base_plan_version": 1' not in text

        dispatch = cli.do_supervise("P", apply=True)
        assert any(x["action"] == "AUTO_DISPATCH" and x["node_id"] == "R1" for x in dispatch["autonomy_applied"])

        _write_proposal(pdir, {
            "base_plan_version": 2,
            "reason": "R1 showed that an independent review step is structurally required",
            "post_hoc": False,
            "evidence_refs": [],
            "operations": [{
                "op": "add_node",
                "node_id": "V1",
                "title": "Independent review",
                "summary": "Independently check the completed R1 conclusion before closure.",
                "parent": "P0",
                "depends_on": ["R1"],
            }],
            "tasks": [{
                "node_id": "V1",
                "role": "reviewer",
                "task": "Independently check the R1 conclusion and report whether it is supported.",
            }],
        })
        _finish("P", "low")
        completed = cli.do_supervise("P", apply=True)
        assert any(x["action"] == "AUTO_COMPLETE" and x["node_id"] == "R1" for x in completed["autonomy_applied"])

        revision_actions = planning.planning_step("P")
        captured = next(x for x in revision_actions if x["action"] == "PLAN_PROPOSAL")
        assert captured["result"] == "captured"
        pending = ResearchPlanGraph(pdir).pending_changes()[0]
        assert pending["base_plan_version"] == 2

        _approve_pending(pdir)
        commit_v3 = planning.planning_step("P")
        assert any(x["action"] == "PLAN_COMMIT" and x.get("plan_version") == 3 for x in commit_v3)
        assert ResearchPlanGraph(pdir).snapshot()["plan_version"] == 3
        assert (pdir / "tasks" / "v3" / "reviewer" / "V1.md").is_file()
        assert "R1" in ProgressLedger(pdir).completed_nodes()
