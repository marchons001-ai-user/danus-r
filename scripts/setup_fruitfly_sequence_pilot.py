#!/usr/bin/env python3
"""Create one bounded, real Researcher pilot for fly-connectome temporal motifs.

Setup only: no model requests, no worker launch, no experiment execution.
Project and science workspace are under the ignored runtime/ directory.
"""

from __future__ import annotations

import json
from pathlib import Path

from danus.core import ResearchPlanGraph
from danus.execution import layout as L
from danus.execution.scaffold import atomic_write, do_new
from danus.orchestration.cli import do_assign


PROJECT = "fruitfly-sequence-pilot"
WORKER = "high"
MODEL = "gpt-6.1-sol"


CHARTER = """# Research Charter — Fruit-fly connectome to temporal prediction

## Main research question
Can directed, region-specialized connectivity motifs suggested by the fruit-fly
connectome inspire a practical trainable architecture for general sequence
forecasting, rather than merely mimicking tasks that a fruit fly performs?
Possible downstream evaluation: AIS ship trajectory forecasting.

## Scope of this one-round pilot
Analyze candidate motifs and falsifiable model hypotheses ONLY. Treat the
biological graph, artificial differentiable implementation, and predictive
benchmark as three distinct objects. Distinguish the evidence available now
from assumptions, intuition, and speculation.

## Success criteria
Produce a coherent Chinese design note describing 2–3 alternative connection
motifs, precise computational implementations, at least one low-cost ablation,
parameter-matched conventional baselines, and explicit failure criteria.
Persist one concise project-level plan through the authorized gm_add Gateway
tool. No numerical performance claims without executed evidence.

## Constraints
- One Researcher round; no Reviewer/Experimenter or Supervisor automation yet.
- Do not download data, install packages, train models, or use a GPU.
- No changes to Danus-R source, Research Plan, or other projects.
- Do not call fact_submit or assert biological claims as verified facts.
- No fabricated papers, datasets, connectome statistics, or experimental results.
- If primary literature is not accessible, mark its details "待核实" and list
  exact evidence that should be retrieved in a later source-checking stage.
- Do not confuse "fruit-fly-inspired structural inductive bias" with a
  biologically faithful whole-brain simulation.
"""


TASK = """# Pilot R1 — Directed fly-connectome motifs for sequence modeling

You are the Researcher, running a bounded FIRST ONLINE SCIENTIFIC PILOT.
The initial motivation is the user's interest in fruit-fly functional brain
regions, distinctive inter-region connections, and temporal sequence prediction,
potentially applied later to AIS vessel trajectories.

Complete these steps:
1. Identify 2–3 materially different candidate architectural motifs inspired
   by directed inter-region wiring, recurrence, and specialized information
   pathways. For each, label every biological premise ESTABLISHED only with
   actual accessible evidence; otherwise use UNVERIFIED/HYPOTHESIS.
2. Translate each motif into an explicit trainable neural computation:
   state/input/output shapes, directed connections, delay or recurrence handling,
   and whether gradients require BPTT, fixed unrolling, or implicit differentiation.
3. Contrast mechanisms with a simple GRU/LSTM and a standard Transformer or
   residual sequence model. Focus on parameter-matched controls and compute.
4. Recommend ONE smallest falsifiable next experiment: dataset and split
   prerequisites, baselines, measured metrics, one ablation, compute/time risks,
   and what negative result would reject the proposed advantage.
5. Save a Chinese technical note with headings and a summary comparison table
   at workspace/fly_temporal_motifs_v1.md. No numerical results are to be invented.
6. Use authorized MCP gm_add exactly once, kind=plan, to record a concise
   evidence-status-labeled summary and the workspace note path.

DO NOT: install dependencies; train anything; perform unrestricted browsing;
call fact_submit; modify plans, assignments, or project control files; write
outside workspace and your own notes; spawn another agent or worker.

If useful primary sources are inaccessible, proceed with clearly marked
unverified design hypotheses; do not hallucinate citations.
Once the note is written AND gm_add has succeeded, end the final response with:
DANUS_OUTCOME: COMPLETE
If the MCP Gateway is not available, report BLOCKED instead of pretending that
the tool call succeeded.
"""


def main() -> int:
    project_dir = L.project_dir(PROJECT)
    workspace = L.repo_root() / "runtime" / "research" / PROJECT
    if project_dir.exists() or workspace.exists():
        raise SystemExit(
            f"Refusing overwrite: project_exists={project_dir.exists()}, "
            f"workspace_exists={workspace.exists()}. Inspect existing pilot first."
        )
    workspace.mkdir(parents=True, exist_ok=False)
    do_new(PROJECT, roles="high:1", model=MODEL, workspace=str(workspace))
    atomic_write(
        L.worker_dir(PROJECT, WORKER) / L.ROLE_FILE,
        f"MODEL={MODEL}\nREASONING_EFFORT=high\nROLE=researcher\n"
        f"DANUS_AUTHOR={WORKER}\nBACKEND=chatgpt\n",
    )
    atomic_write(L.research_charter(PROJECT), CHARTER)
    atomic_write(workspace / "RESEARCH_BRIEF.md", CHARTER)
    graph = ResearchPlanGraph.create(
        project_dir,
        node_id="R1",
        title="Fly-connectome temporal motifs",
        summary="Bounded architecture hypotheses and a falsifiable sequence pilot.",
    )
    assignment = do_assign(f"{PROJECT}/{WORKER}", TASK, node_id="R1")
    result = {
        "project": PROJECT,
        "project_dir": str(project_dir),
        "workspace": str(workspace),
        "model": MODEL,
        "role": "researcher",
        "worker": WORKER,
        "plan_version": graph.snapshot()["plan_version"],
        "assignment_id": assignment["assignment_id"],
        "task_file": assignment["task_file"],
        "started": False,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
