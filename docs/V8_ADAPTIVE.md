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


## V8 independent experiment Validation Gate (2026-10-13)

V8 keeps **three orthogonal dimensions**:

- Worker execution: task COMPLETE / BLOCKED.
- Evidence validation: PASSED / INCOMPLETE / FAILED; created by host code,
  not from Experimenter's textual assertion or its self-written checks.json.
- Independent scientific review: ACCEPT / INCONCLUSIVE / REVISE / REJECT.
  ACCEPT means the scoped scientific interpretation was reviewed, **not** that
  the positive hypothesis was confirmed.

At Researcher proposal submission, an optional JSON field \`required_checks\`
contains a list of executable check specifications. The full check list becomes
part of the proposal's content-addressed identity; Reviewer approval therefore
covers the **exact** check list. Experimenter cannot modify it at runtime.
For new experiments, **no executable check list means INCOMPLETE**, not PASS.

Supported deterministic check kinds:

\`\`\`json
[
  {"id":"integrity","kind":"asset_integrity"},
  {"id":"row_count","kind":"csv_rows","path":"run_v2/metrics.csv","expected_rows":256},
  {"id":"mean","kind":"csv_mean","path":"run_v2/metrics.csv",
   "column":"mse","report_path":"run_v2/report.json",
   "report_key":"mse","abs_tolerance":1e-9}
]
\`\`\`

Checks execute in the trusted Danus Python process on newly registered,
hashed files; the CSV checks read the actual bytes rather than accepting a
model-produced PASS flag. Each \`VALIDATION\` event records the checked IDs,
PASSED/INCOMPLETE/FAILED results and specific errors. Experimenter COMPLETE
still records raw observations even on failure; they are **not** promoted to
verified evidence. The generated state, V8 status JSON and existing TUI show
the machine-derived verdict. A corrected, independently approved experiment
may supersede an older failed run; historical evidence and failures are kept.

V8 \`finish\` refuses if the latest experiment has no PASSED Validation Gate
result. Supervisor-L/H receives the exact validation debt and may coordinate
additional tests against frozen inputs, a new Reviewer-approved validation-only
experiment, or a corrected experiment. This is **not** a forced R1/V1/E1 DAG.
If a repair is impossible or budgets expire, HIGH may HALT with an explicit
unresolved-validation reason. Do not transform incomplete validation into an
ordinary scientific INCONCLUSIVE. An inconclusive **validated** experiment
may finish after the normal independent final-review gate.

**What PASSED does not prove:** these generic checks cannot prove causality,
mask invariance, model alignment or domain-specific scientific correctness on
their own. A proposal that requires bespoke checks must define a **genuine
executable, independently approved** validator; until supported, the result
must remain INCOMPLETE. Reviewer is responsible for checking that the stated
scientific invariants are covered by the executable suite. The current stage
does not accept an Experimenter-supplied checks.json as proof.

### V7 Evidence reuse and boundary

Optional \`v7_evidence\` check reuses
\`danus.verify.empirical.check_empirical_evidence\` and accepts only pinned
pre-existing \`evidence_ids\` (list of 16-hex IDs), \`protocol_id\` (16-hex ID)
and a nonempty \`scope\`. V7 checks protocol lineage, immutable Evidence
Record identity, artifact SHA/size, and any already-frozen metric reducer.
This deliberately does **not** call V7's Plan DAG or scheduler.

The V7 runtime has a stricter **pre-execution frozen bundle** contract.
V8's current write-enabled Experimenter can create scripts at execution time,
so it cannot automatically claim to have used that V7 frozen execution
contract. Fully running newly authored experiments through V7's immutable
bundle / execution runtime would require an explicit **implementation freeze
and independent approval before execution**, and is not falsely represented
as implemented by this validation gate. V7's empirical semantic fact-verifier
and Fact Graph promotion are also separate later actions, never implied by
Experimenter COMPLETE or Validation PASSED.

Offline checks:

\`\`\`bash
python -m pytest -q danus/tests/test_v8_adaptive.py danus/tests/test_v8_validation.py
\`\`\`

Existing completed V8 projects remain auditable as historical records; the
new validation gate does not retroactively claim their results are PASSED.
Test the gate with a **new** project, not by reopening a finished pilot.


## V8 input staging and verifiable handoff (2026-10-14)

Raw scientific inputs have a separate pre-execution provenance gate.
The existing Experimenter output registration and Validation Gate are unchanged.
Do not manually copy external data into workspace/input before staging.

For a new project with external inputs:

1. Create a new project with its fixed goal.
2. Run the V8 stage-inputs CLI ONCE, before the first task dispatch.
3. Run V8 normally. All Workers can read the staged provenance manifest.
   The host checks the input and manifest hashes before each Supervisor step,
   before Worker execution and after the Worker returns.

Example for the existing FlyCX-Traj extracted CX CSVs:

    python -m danus.v8 new v8-cx-pb-delta7-20261014 --goal-file docs/pilots/v8-cx-pb-delta7-20261013.md
    python -m danus.v8 stage-inputs v8-cx-pb-delta7-20261014 --source-dir "$HOME/research/FlyCX-Traj/data/cx" --dataset-label "hemibrain:v1.1" --extraction-script "$HOME/research/FlyCX-Traj/extract_cx.py" --files neurons.csv connections.csv roi_connections.csv
    python -m danus.v8 run v8-cx-pb-delta7-20261014 --max-steps 16 --worker-timeout 600

Staging copies explicitly selected source files into workspace/input.
It checks SHA-256 before and after copying; saves input/PROVENANCE.json
with file hashes, source paths, operator-declared dataset label, best-effort
Git commit and optional extraction-script/summary references. Immutable
INPUT_STAGE events bind the manifest SHA and input file hashes/byte sizes.
The Worker cannot register staged files as new experiment output artifacts;
changing input or manifest bytes causes subsequent execution to fail closed.
Status and TUI expose the staged input count.

Scope of proof: byte identity with named *local files at staging time*.
It does NOT independently authenticate the upstream Janelia release,
extraction correctness, reconstructed cell types or synapse counts.
The source repository's Git HEAD identifies its revision when staging
occurred, not a guarantee that CSVs were generated by that commit.
Workers must not describe this local handoff as independently verified
upstream provenance.

Staging refuses requests after ANY task dispatch and refuses to overwrite
existing input directories. Use a new project when changing external data.
Projects that do not use external inputs keep their historical behavior.
Previously completed projects are not retrospectively certified.

Offline targeted tests:

    python -m pytest -q danus/tests/test_v8_inputs.py danus/tests/test_v8_validation.py danus/tests/test_v8_adaptive.py


## V8 Reviewer feedback loop (2026-10-14)

The final Reviewer now receives the **full currently approved experiment
proposal** (including the proposed executable validation checks), not merely
Experimenter's last 3000-character summary. Reviewer, rather than Supervisor,
decides scientific adequacy. Supervisor only routes to a useful role.

After interpreting the latest evidence, Reviewer uses the existing verdicts:
- ACCEPT: a scoped claim is adequately supported; not synonymous with a
  positive hypothesis or flawless code.
- INCONCLUSIVE: evidence is adequate, but scientific outcome uncertain.
- REVISE: **blocking defect** requiring actionable correction; must provide
  one or more structured issues.
- REJECT: rejects the scientific claim/protocol.

A REVISE result includes `issues` with:
`problem, impact, required_action, target_role (researcher/experimenter),
evidence`. Runtime assigns each issue a stable ID; it is recorded in
the immutable event log and passed directly to next Worker tasks and the
Supervisor. Corrective experiment execution still requires an independently
approved (possibly revised) proposal and must not overwrite previous assets.
Reviewer must reassess fixes, and in a later final review explicitly record
`resolved_issues` with each issue ID, `status=RESOLVED|WAIVED`,
`reason`, and `evidence`. The runner does not let an ACCEPT silently dismiss
outstanding issues, or FINISH while any blocking Reviewer issue remains OPEN.

For an optional/redundant protocol procedure, Reviewer may report
`accepted_deviations` with `item, reason, evidence`. A prior blocking issue
may be explicitly WAIVED if Reviewer documents a bounded logical ablation:
which possible error that procedure was meant to reveal, which existing
**independent evidence** already excludes it, and the scope. Neither a waiver
nor prose changes the host Validation Gate's PASSED/FAILED state. Missing
machine-mandatory checks still require actual host verification. This avoids
adding generic tests merely to maximize check counts.

The existing read-only TUI shows latest review verdict and number of open
Reviewer issues; the generated PROJECT_STATE.md lists each pending correction.
Only three small offline scenarios are needed before another real research
pilot:

    python -m pytest -q danus/tests/test_v8_review_feedback.py danus/tests/test_v8_adaptive.py danus/tests/test_v8_validation.py danus/tests/test_v8_inputs.py

Historic V8 projects retain their original recorded decisions; feedback
fields are backward-compatible and do not retroactively change past results.
