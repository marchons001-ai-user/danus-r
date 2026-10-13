# Danus-R V8 adaptive orchestration (isolated pilot)

V8 is a standalone execution path: python -m danus.v8.
It does not change or auto-migrate any V7 project or use the V7 Plan-DAG scheduler.

## Scope

- Luna Low selects the next task. When it cannot select a legal useful action,
  Sol Medium receives the same current state plus the error reason. Both are
  subject to the same deterministic action gates.
- One Researcher, Reviewer or Experimenter runs at a time. A worker may request
  information from any other role or Supervisor. One pending request is allowed
  per waiting worker task; after a reply it may ask a further question.
- The research goal is fixed. EVENTS.jsonl is the append-only authoritative
  ledger, PROJECT_STATE.md and ASSET_INDEX.json are generated views.
- Researcher proposes an experiment. Independent Reviewer must approve the exact
  proposal ID before an Experimenter is assigned a writable experiment turn.
  Independent final Reviewer assessment is required before project completion.
- Uses the existing Codex launcher and sandbox: read-only for Supervisors,
  Researcher, Reviewer and Experimenter answer turns; workspace-write only for
  approved Experimenter experiment turns.
- The per-project runner lock prevents simultaneous Supervisor processes.
  Explicit recovery allows interrupted read-only tasks, but not blind reruns
  of experiments.

## Deliberate limitations (NOT a V7 replacement yet)

- V8 records hashed workspace artifacts but does not yet run the V7 frozen
  Evidence protocol, approved implementation-bundle, or deterministic metric
  reducer. A V8 artifact hash is NOT an independently verified V7 Evidence
  Record. It also does not auto-promote claims to the verified Fact Graph.
- V8 does not invoke the V7 Gateway MCP or reuse its TUI. Those are currently
  bound to V7 Plan/Assignment semantics. V8 deterministic gates enforce
  task ownership, experiment approval and file integrity; this does not
  constitute a complete capability sandbox. Use a dedicated workspace and
  trusted reference inputs.
- Available model names depend on your Codex account. Defaults are Luna Low,
  Sol Medium and role-specific model choices; environment overrides supported.
- Restarting V8 never silently reruns an interrupted experiment.
- A scientific result is not declared verified merely because Supervisor
  finished. Final reports must state actual evidence and limitations.

## CLI

Create a new project:

    python -m danus.v8 new project-name --goal-file /absolute/path/goal.md --reference /path/to/reference

Inspect:

    python -m danus.v8 status project-name

Execute exactly one Supervisor decision/worker turn:

    python -m danus.v8 step project-name

Continue for a bounded pilot:

    python -m danus.v8 run project-name --max-steps 20 --worker-timeout 600

The default --max-steps 0 means unlimited steps. A project is stored under
DANUS_RUNTIME/v8/project-name; source V7 runtime projects are untouched.

If a stopped project is genuinely resolved:

    python -m danus.v8 unblock project-name --reason "Operator resolved condition"

If an interrupted read-only task needs replay:

    python -m danus.v8 recover project-name --task-id task-N

## Offline smoke test

    python -m pytest -q tests/test_v8_adaptive.py

Three scenarios cover multirole follow-up communication, mandatory experiment
review and immutable asset integrity, and low-to-high Supervisor escalation.
Actual Codex invocation and research execution must still be verified on WSL.

## V8 TUI

The original Textual interface now supports a V8 read-only event/log adapter:

    python -m danus.tui project-name --v8

Task tree, workers, experiment turns, requests and Codex activity are visible.
Legacy V7 pause, guidance and interrupt controls are disabled in V8 mode.

## Recovery of an already completed model turn misparsed by older V8

Early V8 erroneously used the generic 8K-truncated context summary as the
machine-readable JSON worker result. If the Codex log contains a full
turn.completed and JSON final message, a read-only worker may have been
recorded as BLOCKED with JSONDecodeError. The fixed V8 parser reads the
complete final agent_message.

Do not rerun that research. First wait for any currently RUNNING worker to
finish, or explicitly stop and reconcile it only after the process has exited.
Then the original completed read-only result can be restored from its hashed
Codex log:

    python -m danus.v8 salvage project-name --task-id task-N

The command requires a prior JSONDecodeError blocker, exactly one finished
Codex log, no concurrently running worker and a non-experiment task. It
preserves the old events and records a RECOVERY audit event. It never
re-executes the model.

If a worker was interrupted rather than completed, salvage cannot invent a
missing answer. First reconcile the truly stopped RUNNING assignment:

    python -m danus.v8 reconcile project-name --task-id task-N --reason "Operator inspected process and incomplete output"

A worker error within a Supervisor step no longer causes automatic
redispatch of a second scientific worker in the same step.

## Corrective-proposal recovery and scientific-review freshness (2026-10-13)

V8 now accepts experiment proposals up to 32,768 characters (the original
16,000-character limit rejected a complete 19,689-character corrective
proposal). Completed read-only Worker responses rejected specifically by
the historical proposal-length check can be restored **without a model call**:

    python -m danus.v8 salvage v8-flycx-ring-20261013 --task-id task-38

Use only once, after the run has exited and no other Worker is active.
The source log must contain a completed model turn and valid JSON. An audit
RECOVERY event records the source-log SHA-256. The corrected proposal enters
PENDING review status; it is not automatically approved or executed.

There are two different forms of independent review:
- Early technical audit: Reviewer purpose=work may inspect existing experimental
  evidence and reject its assumptions or implementation at any time.
- Final scientific review: Reviewer purpose=final_review evaluates the
  Researcher's interpretation of the latest completed experiment. When
  experiments exist, Researcher purpose=interpret must complete after the
  latest experimental result before final_review can be dispatched.

Completing a later scientific task or registering new experimental artifacts
makes any earlier final review stale for FINISH authorization. These are
general evidence-freshness rules, not a fixed role/task-order state machine.
Existing historical Reviewer REVISE reports remain available; they do not
authorize project completion.
