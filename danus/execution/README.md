# danus/execution — the worker swarm (round loop + scaffolding + layout)

Where autonomous worker sessions run. This module owns the on-disk **layout**,
project/worker **scaffolding**, and the per-worker **round loop**. The `danus` CLI
(`danus/orchestration`) is a thin UX layer over this lifecycle.

```
danus/execution/
  layout.py     paths + names; WorkerLayout; role-selected harness paths
  scaffold.py   do_new, managed harness links, role-specific .codex config, spawn_loop
  loop.py       kickoff prompt, run_round, stop conditions, status
  __main__.py   `python -m danus.execution <worker_dir>` → loop.main
  tests/        offline execution + harness tests
```

## On-disk layout (`layout.py`)

`<agents_root>/<project>/` holds shared project stores plus `project.json` and
`RESEARCH_CHARTER.md`; each `workers/<worker>/` is a model working directory with
`AGENTS.md`, optional `.agents/skills`, `.codex/config.toml`, `TASK.md`, Local
Memory, and lifecycle control files.

Harness selection is role-dependent:

- legacy mathematical worker → `worker.md` + `agents/skills/worker`;
- Researcher/Reviewer → `scientific_worker.md` + injected role contract + Research
  Charter, with no default skills;
- Experimenter → the same scientific spine/contract/Charter plus only
  `agents/skills/experimenter/ponytail` for final code cleanup.

The loop re-syncs Danus-managed harness symlinks and the gateway `DANUS_ROLE` from
`.role` on every worker start. Role-specific gateway permissions are defined once
in `danus/gateway/roles.py`: Researcher can submit candidate facts and search
literature, Reviewer can review/search but cannot submit facts, and Experimenter
has only shared-memory/fact-read tools by default. Legacy mathematical workers keep
the historical `worker` tool surface.

## The round loop (`loop.py`)

A **round = one continuation session** that resumes from persisted project state,
not one increment. The loop is launched detached in its own process group. Stop
conditions are checked at round boundaries: `.stop`, `.run_deadline`,
`DANUS_MAX_ROUNDS`, and `DANUS_MAX_CONSEC_FAILURES`. `.status.json` is written
atomically. Resumability is continuity in the stores rather than process state.

Legacy workers retain the historical adaptive mathematical skill loop. Scientific
roles receive a smaller kickoff focused on `TASK.md`, the common scientific spine,
their role contract, the Charter, relevant project state, and only the tool names
actually exposed to that role. The Experimenter's single Ponytail skill is also
exposed through the worker-home skill link for Codex. Scientific DSH rounds are
refused; Reviewer auto selects isolated Codex/ChatGPT. Legacy ROLE=worker retains
its separate historical execution path without requiring an Assignment.

Runnable protocols bind the frozen bundle and a canonical execution runtime
identity: Python implementation/version, executable hash, environment paths and
installed dependency RECORD digest. Recorded dependency file hashes are checked
before execution. Experiment and reducer execution reject a changed runtime;
historical protocols remain readable but need a new freeze before execution.

## Connects to

The loop itself does not write truth stores. Workers use the gateway tools exposed
to them; accepted facts enter the Scientific Fact Graph only through the unified
verification gate. The loop records the last returned `fact_id` in status when one
appears in the round log.

## Tests

`python -m pytest danus/execution/` is offline; no model or network is required.
