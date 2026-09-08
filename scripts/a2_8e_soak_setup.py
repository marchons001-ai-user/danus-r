#!/usr/bin/env python3
"""Create the deterministic A2-8e autonomous long-run soak fixture.

This script creates only test inputs: workspace files, a frozen E1 protocol,
scientific roles, Research Charter, and RESEARCH_BRIEF.md. It deliberately does
not create a Research Plan, assign a node, or start a worker. The actual soak must
start with ``bin/danus research PROJECT`` and run without manual node operations.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from a2_7p_soak_setup import _REDUCER, _RUN_CASE, _SIGMAS_E1, _role
from danus.core import EvidenceStore, ProgressLedger
from danus.execution import layout as L
from danus.execution.scaffold import atomic_write, do_new


_CHARTER = """# Research Charter — A2-8e Autonomous Long-Run Soak

This project is an integration soak for Danus-R autonomy, not an open-ended
scientific study.

- Do not browse the web, install dependencies, or expand the literature search.
- Reuse the supplied deterministic fixture; do not modify run_case.py or reduce_metrics.py.
- Preserve all frozen E1 cells, deterministic seeds, RMSE metric, and negative/null outcomes.
- Do not tune the five-sample moving-average window after seeing results.
- Research Plan creation, revision, task dispatch, node closure, and Plan commit must go through the autonomous Danus-R control plane.
- Do not ask the human to assign, complete, reopen, start, stop, pause, resume, or otherwise operate a Plan node.
- Any result-conditioned structural revision must be post-hoc and cite the immutable E1 Evidence Record.
- If a generated Plan proposal is malformed, use the existing deterministic validator/PLAN_REPAIR path; never silently guess or auto-correct scientific structure.
- If control state, protocol identity, evidence, or an approved task is inconsistent, surface the inconsistency instead of repairing around it.
"""


def _contract(project_dir: Path, protocol_id: str) -> tuple[str, dict[str, object]]:
    fields: dict[str, object] = {
        "goal": (
            f"Execute frozen protocol {protocol_id} through the existing Danus Experiment Runtime "
            f"for control project {project_dir}, and return its immutable evidence without modifying the fixture.\nEXPERIMENT_PROTOCOL_ID={protocol_id}"
        ),
        "must_preserve_invariants": [
            f"Run exactly protocol {protocol_id}; use only its five sigma cells and hash-pinned reducer.",
            "Use raw-current persistence as baseline and the five-sample trailing moving average as treatment.",
            "Use the deterministic seeds and existing run_case.py/reduce_metrics.py unchanged; retain every cell.",
        ],
        "forbidden_behaviors": [
            "Do not tune the window, add or remove cells, modify fixture code, install dependencies, or drop unfavorable results.",
            "Do not replace RMSE or reinterpret the universal-improvement hypothesis after seeing results.",
        ],
        "acceptance_conditions": [
            f"Call Gateway experiment_run(protocol_id={protocol_id}) successfully.",
            "The Experiment Runtime checkpoint records all five completed units and one immutable Evidence Record.",
            "Report the evidence_id, primary metrics, full matrix coverage, failures, and every negative cell without making a broader claim.",
        ],
    }
    task = ProgressLedger.implementation_task(
        goal=str(fields["goal"]),
        must_preserve_invariants=list(fields["must_preserve_invariants"]),
        forbidden_behaviors=list(fields["forbidden_behaviors"]),
        acceptance_conditions=list(fields["acceptance_conditions"]),
    )
    return task, fields


def _brief(project_dir: Path, protocol_id: str, e1_task: str) -> str:
    return f"""# A2-8e Autonomous Long-Run Soak Brief

This is a deterministic end-to-end autonomy acceptance run. The human will start
the project once with `bin/danus research` and will perform no manual Plan-node
operations afterward.

Scientific question:
On the supplied deterministic signal, does a five-sample trailing moving average
reduce one-step prediction RMSE versus raw-current persistence at every frozen
noise level in E1 protocol `{protocol_id}`?

The initial Research Plan must be the smallest route that still exercises the
three scientific roles and the implementation gate. Under root P0, create exactly
these five executable sibling nodes and no other initial nodes:

1. R1 — Researcher — no dependencies. Frame the frozen universal-improvement
   hypothesis, inspect the supplied protocol, persist a concise Research Proposal,
   and submit exactly one implementation contract for E1 at the current Plan
   version. The contract fields must be byte-for-byte equivalent to the E1 task
   shown below. Do not run E1.
2. V1 — Reviewer — depends_on [R1]. Read the one pending E1 implementation
   contract. APPROVE only if it matches the frozen protocol/task exactly and
   preserves every cell/outcome. Do not run E1.
3. E1 — Experimenter — depends_on [V1]. Its exact task text must be the
   Implementation Contract block below. Execute it once through the existing
   Experiment Runtime and finish only after immutable evidence exists.
4. V2 — Reviewer — depends_on [E1]. Audit the E1 checkpoint, Evidence Record, and
   all five raw artifacts mechanically. Record the evidence_id and whether the
   universal-improvement hypothesis survived. Do not revise Plan.
5. R2 — Researcher — depends_on [V2]. Read V2 and the E1 Evidence Record. If the
   universal-improvement hypothesis is falsified, write exactly one Autonomous
   Plan Revision handoff against the then-current committed Plan version:
   - post_hoc=true;
   - evidence_refs contains exactly the E1 evidence_id;
   - add V3 (Reviewer) under P0, depends_on [R2], title `Final evidence audit`,
     summary `Audit the bounded interpretation and revision provenance.`;
   - add R3 (Researcher) under P0, depends_on [V3], title `Final bounded claim`,
     summary `Form the final scoped claim without more experiments.`;
   - provide exact tasks only for V3 and R3.
   V3 must independently verify the E1 evidence, the falsification decision, and
   the post-hoc evidence provenance without adding experiments. R3 must persist
   the narrowest final claim supported by E1, explicitly state what is not
   established outside this fixture, create no further Plan proposal, and run no
   experiment.
   If E1 unexpectedly supports the universal hypothesis or evidence is internally
   inconsistent, do not fabricate the revision; finish NEEDS_REVIEW.

The result-conditioned V3/R3 addition is deliberately absent from the initial
Plan. Its later creation is required to validate autonomous Plan v2 -> v3
revision and versioned task materialization.

E1 exact task text begins after the marker and must be copied exactly as the E1
task spec (a final newline is allowed):
---BEGIN E1 EXACT TASK---
{e1_task.rstrip()}
---END E1 EXACT TASK---

Acceptance requires the entire project to reach clean COMPLETE without the human
using assign/start/stop/pause/resume/guide/interrupt or
`supervise --complete-node/--reopen-node`. Read-only status inspection is allowed.
"""


def setup_soak(project: str, workspace: Path) -> dict[str, object]:
    project = project.strip()
    if not project:
        raise SystemExit("project name is empty")
    project_dir = L.project_dir(project)
    workspace = workspace.expanduser().resolve()
    if project_dir.exists():
        raise SystemExit(f"project already exists: {project_dir}")
    if workspace.exists():
        raise SystemExit(f"workspace already exists: {workspace}")

    workspace.mkdir(parents=True)
    run_case = workspace / "run_case.py"
    reducer = workspace / "reduce_metrics.py"
    atomic_write(run_case, _RUN_CASE)
    atomic_write(reducer, _REDUCER)

    do_new(project, roles="low:3", model="gpt-5.6-terra", workspace=str(workspace))
    _role(project, "low", "researcher")
    _role(project, "low2", "reviewer", backend="auto")
    _role(project, "low3", "experimenter")
    atomic_write(L.research_charter(project), _CHARTER)

    store = EvidenceStore(project_dir)
    runner_ref = {**store.artifact_ref(run_case), "path": "run_case.py"}
    reducer_ref = {**store.artifact_ref(reducer), "path": "reduce_metrics.py"}
    units = []
    for sigma in _SIGMAS_E1:
        tag = f"{int(round(sigma * 100)):02d}"
        artifact = f"runs/e1_sigma_{tag}.json"
        units.append(
            {
                "id": f"sigma-{tag}",
                "argv": [sys.executable, "run_case.py", f"{sigma:.2f}", artifact],
                "artifacts": [artifact],
                "inputs": [runner_ref],
            }
        )
    protocol_id = store.freeze_protocol(
        question="Does five-sample trailing averaging improve one-step RMSE at every frozen noise level?",
        hypothesis="The five-sample trailing moving average has lower RMSE than raw-current persistence in every E1 cell.",
        matrix={"units": units},
        primary_metrics=["improved_cells", "max_filtered_minus_raw", "min_filtered_minus_raw"],
        decision_rule="Accept universal improvement only if all five cells have filtered_rmse < raw_rmse.",
        author="soak-fixture",
        metric_reducer={**reducer_ref, "function": "reduce_metrics"},
        execution_files=["run_case.py"], source_root=workspace,
    )

    e1_task, _ = _contract(project_dir, protocol_id)
    brief_path = project_dir / "RESEARCH_BRIEF.md"
    atomic_write(brief_path, _brief(project_dir, protocol_id, e1_task))

    if (project_dir / "plan" / "current.json").exists():
        raise RuntimeError("A2-8e setup must not create a Research Plan")
    if (project_dir / "progress" / "ledger.jsonl").exists():
        raise RuntimeError("A2-8e setup must not create Progress assignments")

    manifest: dict[str, object] = {
        "project": project,
        "project_dir": str(project_dir),
        "workspace": str(workspace),
        "brief": str(brief_path),
        "e1_protocol_id": protocol_id,
        "e1_task_sha256": ProgressLedger.task_sha256(e1_task),
        "workers": {"researcher": "low", "reviewer": "low2", "experimenter": "low3"},
        "plan_created_by_fixture": False,
        "assignments_created_by_fixture": 0,
    }
    soak_dir = project_dir / "soak"
    soak_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(
        soak_dir / "fixture.json",
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the A2-8e autonomous long-run soak fixture")
    parser.add_argument("--project", default="a2-8e-soak")
    parser.add_argument("--workspace", default=None)
    args = parser.parse_args()

    workspace = (
        Path(args.workspace)
        if args.workspace
        else L.repo_root() / "runtime" / "soak" / args.project
    )
    print(json.dumps(setup_soak(args.project, workspace), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
