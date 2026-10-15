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


## V8 JSON response length budgets and actionable diagnostics (2026-10-14)

An observed real-science PFL3 pilot stopped at final Reviewer with
`ValueError: issue evidence must be nonempty text <= 1500 characters`.
That error was ambiguous: evidence could be absent, blank, wrong type or
too long; from the TUI alone it is **not** correct to infer overlength.
A Reviewer model turn was executed, but its result was rejected before
becoming an accepted final review. Earlier input provenance, approved
experiment assets and `VALIDATION: PASSED` remain recorded.

V8 now derives one **soft 70% ceiling table** from the existing hard text
limits and includes it in **every Supervisor and Worker prompt**. Lengths
are Python Unicode string lengths, not tokens or UTF-8 bytes:

| Model-output field | Enforced max (characters) | Suggested ceiling (70%) |
|---|---:|---:|
| Supervisor new task / answer text | 12000 | 8400 |
| Supervisor decision reason | 1800 | 1260 |
| Researcher proposal text | 32768 | 22937 |
| Worker summary, including Reviewer summary | 32000 | 22400 |
| Worker request question | 3000 | 2100 |
| Worker request reason | 1800 | 1260 |
| Each Reviewer issue field: problem, impact, required_action, evidence | 1500 | 1050 |
| Each Reviewer resolution reason / evidence | 1500 | 1050 |
| Each Reviewer accepted deviation item / reason / evidence | 1500 | 1050 |
| Artifact description (truncated at hard limit) | 500 | 350 |
| Each request reference string | 256 | 179 |

These are **ceilings, not targets to fill**. Prefer the existing shorter
model writing guidance: a typical protocol around <=10000 characters,
summary <=5000, request question <=1500, request reason <=800. No
information relevant to scientific soundness should be removed only to
satisfy the soft guide; substantive evidence can be cited from registered
artifacts. List counts are separate constraints: at most 12
Reviewer issues/resolutions/deviations, 16 required checks, 12 request
references and 24 registered artifacts.

Structured Reviewer `REVISE` items require all five keys:
`problem, impact, required_action, target_role, evidence`; all four text
fields must be **nonblank**, while `target_role` is either researcher or
experimenter. Missing information is **not** remedied by inserting a
fabricated evidence claim. Use a concrete observation or cite where the
reported artifact omits a required item. `resolved_issues` require
`id,status,reason,evidence`, and `accepted_deviations` require
`item,reason,evidence`.

The host validator now reports **missing/non-string**, **blank**, or
**actual length N > cap** distinctly. Neither the limits nor scientific
review authority are loosened. There is no blind string truncation and
no silent auto-waiver of a required field.

To recover after a schema-rejected **read-only Reviewer task**, keep all
completed Experimenter outputs and original logs. Update the code, then
continue the SAME V8 project with `danus.v8 run` and append the run log
using `tee -a`; the Supervisor may schedule a *new final review* using
existing evidence. Do **not** salvage an invalid original Reviewer JSON
or rerun the experiment just to fix a review output field. Older BLOCKED
Reviewer attempts remain in the event history.


## V8 verified PFL3 review-evidence and long-context transport fixes (2026-10-14)

Real incident diagnosis:
- task-28 Reviewer returned a valid REVISE with five substantive issues.
  Every `evidence` value was a **list of 2-3 strings** (e.g. source file
  locations), while V8 required a single string. The failure was **not**
  caused by character count or absent evidence.
- Later task-32 completed REVISE, recording five issues; task-36 Researcher
  completed work on those issues. Next, Supervisor Popen failed with Linux
  `E2BIG`: measured V8 prompt = **134,583 UTF-8 bytes** versus Linux
  per-argument cap **131,072 bytes**. Repeating Low/High cannot fix E2BIG.
  That historical HALT is an execution transport failure, not a scientific
  rejection, and must be explicitly unblocked after updating the code.

Minimal fixes in `danus/v8.py`:

1. Reviewer evidence (new issues, resolutions, accepted deviations) can be
   either one nonblank string or 1-8 nonblank strings. V8 validates each
   list entry and the full joined value against the existing **1,500
   character hard ceiling**, joining in order with newline. The source
   citations are not truncated or fabricated; malformed objects, blanks,
   oversized evidence and empty/oversized lists still fail.
2. V8 `_call` invokes `codex exec ... --json -` and sends the full UTF-8
   prompt using `Popen.communicate(input=..., timeout=...)`. Both bundled
   `bin/codex` and `bin/codex-dsh` accept the stdin `-` sentinel, so
   neither Supervisor nor Worker embeds the prompt in a single argv entry.
   Existing process-group timeout and sandbox flags are preserved.
3. The Supervisor gets a compact routing view: bounded recent task
   summaries, proposal IDs/status/check names rather than full proposal
   contents, current open issue IDs/fix requests, and bounded recent
   event excerpts. The full proposals and evidence remain immutable in the
   event log and available to scientific Workers in their assignments.
   This reduces token duplication without hiding essential review debt.
4. A future OS-level Supervisor launch error records a precise
   `SUPERVISOR_LAUNCH_ERROR` HALT **once**, rather than generating repeated
   denied scientific decisions.

Offline regression:

    python -m pytest -q danus/tests/test_v8_transport_and_evidence.py danus/tests/test_v8_review_feedback.py danus/tests/test_v8_adaptive.py danus/tests/test_v8_validation.py danus/tests/test_v8_inputs.py

Do not restage data, recover task-28, or rerun task-11 merely to resume
`v8-pfl3-steering-20261014`. After locally verifying the new branch,
execute `python -m danus.v8 unblock v8-pfl3-steering-20261014 --reason
"Fixed supervisor argv E2BIG and Reviewer evidence schema; preserve all five
open issues and verified experimental artifacts"`, then invoke the normal
V8 run command with a bounded step count and **append** to existing logs.
The system must route outstanding Reviewer scientific corrections rather
than waive them silently.


## V8 post-experiment permission denial must not HALT as launch error (2026-10-14)

A PFL3 real-science run successfully completed Reviewer approval task-45 and
corrective Experimenter task-49, then Supervisor tried `final_review` before
a NEW Researcher `interpret`. The deterministic `_check_dispatch` correctly
raised `PermissionError`, but `step()` caught it as `OSError` (the Python
base class of PermissionError) and incorrectly wrote
`SUPERVISOR_LAUNCH_ERROR` + HALT.

This is an orchestration error, NOT a model launch failure and NOT scientific
rejection. Fix: scope `OSError` handling to the `_supervisor()` invocation,
so `PermissionError` from `apply_decision()` creates the usual `DENIED`
event and allows a bounded retry of a different legal action. Similarly,
a genuine model timeout or nonzero exit (RuntimeError) is now recorded as
`SUPERVISOR_MODEL_ERROR` rather than consuming further attempts as if the
model returned illegal decisions. Actual decision-stage filesystem I/O
failures record `SUPERVISOR_DECISION_IO_ERROR`, distinct from launch errors.
Existing authority gates and the requirement to interpret a newly completed
experiment remain unchanged.

The condensed Supervisor state now exposes the authoritative boolean
`latest_experiment_needs_interpretation`; the existing dynamic policy tells
the model to route Researcher purpose=interpret before final_review when true.

Offline focused regressions:

    python -m pytest -q danus/tests/test_v8_supervisor_errors.py danus/tests/test_v8_transport_and_evidence.py danus/tests/test_v8_review_feedback.py danus/tests/test_v8_adaptive.py danus/tests/test_v8_validation.py danus/tests/test_v8_inputs.py

After the tests pass, explicitly `danus.v8 unblock` the EXISTING PFL3 project,
preserve task-49's registered scientific outputs and the five open Reviewer
issues, then continue with `run` and append to the autonomous-run log. Never
restart the corrective experiment just to fix the routing classifier.


## V8 Reviewer issue convergence: preserve distinct defects, stop duplicate debt (2026-10-14)

In the real PFL3 pilot, the 16-step correction sequence expanded from
5 OPEN Reviewer findings to 12 OPEN (2 earlier findings were RESOLVED).
The original V8 rule `REVISE requires explicit actionable issues` forced
each review with unchanged old debt to **register new findings**. Some new
records expressly repeated a previous issue ID. This is bookkeeping
inflation, not evidence of 12 distinct scientific failures.

In final_review, Reviewer now:

- May return `verdict: "REVISE", issues: []` when at least one PREVIOUSLY
  registered Reviewer finding is still OPEN after explicitly recorded
  resolutions. Quote its **existing stable ID** in the review summary.
  No new issue ID is created; earlier evidence and repair instructions remain
  authoritative. REVISE without either new problems or unresolved old
  problems remains invalid.
- Adds a new issue only for a **genuinely distinct** scientific defect,
  with concrete evidence and a new actionable remedy. The validator
  rejects an exact repeat of an OPEN issue's problem+required_action
  and explicit `task-N/issue-M remains OPEN` restatements masquerading
  as new issues. It does NOT use fuzzy semantic similarity to decide that
  two different scientific concerns are equivalent.
- May resolve a *historical duplicated record* with
  `resolved_issues: [{id, status:"DUPLICATE", canonical_id,
  reason, evidence}]`. Only Reviewer makes this scientific/procedural
  equivalence judgment. The `canonical_id` must identify a **different
  still-OPEN original issue**, with a documented reason and evidence.
  The canonical issue must remain OPEN after the review, so the required
  work is not accidentally waived; a DUPLICATE is not scientific
  RESOLVED or WAIVED. Existing event history is untouched. The resulting
  DUPLICATE link is visible in the persisted state.
- Continues to use `RESOLVED` for an actually verified repair and
  `WAIVED` for a scientifically justified logical ablation. Machine
  Validation Gate failures cannot be waived in prose. Supervisor
  continues routing, not deciding which science findings are equivalent.

No global numerical convergence targets, forced reduction in issue count,
review-round ceilings, heuristic embeddings, or special Supervisor-H
stagnation alarms are introduced here. An increase in genuinely distinct
scientific findings is still allowed. The loop can remain INCONCLUSIVE
when the scientific evidence itself is insufficient.

The previous PFL3 runtime keeps its immutable history including the 12
historical OPEN entries. Only a subsequent properly evidence-backed
Reviewer resolution may mark a duplicated record DUPLICATE. It is NOT
safe to delete or auto-merge old review events.

Focused offline tests:

    python -m pytest -q danus/tests/test_v8_review_convergence.py danus/tests/test_v8_review_feedback.py danus/tests/test_v8_adaptive.py

Do not run an additional synthetic scientific task in this repair cycle.
After local tests, resume the existing unhalted PFL3 project only if the
operator accepts its already approved correction_5 proposal and resulting
scientific scope. Otherwise audit the existing outstanding issues first.


## V8 scientific relevance and Researcher correction grouping (2026-10-14)

Reviewer should distinguish evidence flaws that may change the scoped scientific
conclusion, engineering omissions that limit reproducibility, and repeat checks
already covered by independent evidence. Each blocking request should identify
the concrete failure it detects and the smallest justified remedy. An omitted
auxiliary procedure alone does not invalidate an otherwise supported narrow
scientific conclusion; missing decisive evidence is not silently waived.

To avoid one experiment for each repeated finding, Researcher purpose=work may
return `issue_merge: {issue_ids:[...], summary, reason}` referencing 2-12
distinct existing OPEN Reviewer findings. The event remains PENDING until
Supervisor decides `review_issue_merge` with APPROVE or REJECT and a reason.
This is **work grouping**, never scientific resolution: all original Reviewer
findings remain OPEN and only Reviewer can mark RESOLVED, WAIVED or DUPLICATE
with evidence. The complete event history is retained.

A subsequent Researcher corrective proposal may include `issue_merge_id`
only if that grouping is approved and its source findings remain OPEN. The
proposal still requires separate independent Reviewer approval before
Experimenter execution. Historical approved experiment proposals remain valid;
no old Reviewer findings, scientific inputs or experiments are deleted.

Focused local regression: `python -m pytest -q
danus/tests/test_v8_review_convergence.py danus/tests/test_v8_review_feedback.py
danus/tests/test_v8_adaptive.py danus/tests/test_v8_validation.py
danus/tests/test_v8_inputs.py` (one shell line).


## V8 goal-first logical ablation (2026-10-14)

The PFL3 pilot repeatedly produced full correction protocols despite a stable
negative/inconclusive scientific interpretation. The same omitted independent
checker/replay requirements survived successive revisions. Merging issue IDs
alone does not shrink the actual work. V8 therefore changes **worker and
Supervisor policies**, not the event schema, role ownership or finish gates:

- Researcher starts from the fixed hypothesis and defensible claim, then audits
  **distinct failure modes** before proposing corrections: what failure matters,
  which genuinely independent existing evidence covers it, whether to REUSE,
  NARROW, REPAIR or OMIT, and the smallest required remaining repair. Keep this
  assessment brief; do not copy entire prior protocols or inventories.
- Reviewer challenges costly, repeated or nonessential checks during
  `review_experiment`. A full but needlessly broad proposal can be REVISE
  before any Experimenter runs. Never approve an inadequate plan merely
  because it is cheap. In final_review, use existing RESOLVED/WAIVED/DUPLICATE
  and accepted_deviations with concrete evidence where appropriate.
- Supervisor, on observing the **same defect surviving two actual corrective
  attempts**, prioritizes a Researcher necessity/scope reassessment or an
  independent Reviewer audit instead of repackaging another larger plan.

This is **not** permission to erase old Reviewer OPEN findings, waive host
Validation Gate failures, claim that a missing independent replay happened, or
silently alter an already approved experiment. A replacement plan still needs
its own Reviewer approval and the normal experiment validation / final scientific
review. A negative result is valid only insofar as its supporting evidence is
credible; incomplete scientific verification remains visible.

An existing PENDING proposal (including the PFL3 correction_6) remains PENDING.
New Reviewer instructions apply when its independent approval turn is actually
run. Reviewer can APPROVE, REVISE or REJECT based on minimum sufficient evidence;
the code does not retroactively withdraw it or automatically restart an
experiment. The event history and frozen artifacts are untouched.

Focused local check:

    python -m pytest -q danus/tests/test_v8_review_convergence.py danus/tests/test_v8_review_feedback.py danus/tests/test_v8_adaptive.py danus/tests/test_v8_validation.py danus/tests/test_v8_inputs.py

Continue the SAME PFL3 project only after this test passes. Use small run
windows and audit whether genuinely unresolved failure modes and repeated
corrections decrease; do not optimize only for fewer OPEN issue IDs.


## V8 approved-implementation ownership and checker execution receipt (2026-10-15)

Real PFL3 task-237 incorrectly BLOCKED because a new `validation_7/run.py`
was absent, despite the approved plan requiring a new implementation. An
Experimenter experiment turn is workspace-write and **owns implementing** its
Reviewer-approved plan, including creating NEW scripts and entrypoints in the
authorized workspace. Missing planned outputs are not missing prerequisites.
This does not permit changing scientific conditions, frozen/staged files,
required checks, or a protocol explicitly requiring a pre-existing frozen script.
A BLOCKED experiment remains in the historical ledger; no rerun is implied.

The prior PFL3 correction_6 Checker accurately returned exit 1/INCOMPLETE,
but the Host Validation Gate returned PASSED on approved CSV/mean checks alone.
A narrow **implicit checker_execution receipt** now applies to newly completed
experiments whose approved protocol explicitly describes a `--check` command
and `run_log.json`. The Host requires:
- one freshly registered `run_log.json` and unique fresh `run.py` entrypoint
  appearing in the approved Python command;
- no nonzero or incomplete checker/replay/parent exit in the registered run log;
- a completed execution of that exact new entrypoint with exit code zero,
  observed in the Host-owned Codex JSON `command_execution` log (outside the
  writable experiment workspace).

Missing receipts, unrecognized exit fields or missing fresh artifacts cause
`INCOMPLETE`; observed nonzero exits cause `FAILED`. Those outcomes are
added to the ordinary host `VALIDATION` event as `checker_execution`. This
does not mutate approved `required_checks`, launch arbitrary worker code in
the trusted host process, or retroactively rewrite historical PASSED events.
It catches known false positives while retaining the existing scientific
authority boundaries.

**Scope:** This corroborates actual wrapper invocation and exit status,
not independent correctness of the scientific checker. The worker-authored
run log can reveal failure but cannot, by itself, prove successful execution.
Reviewer must still inspect independent replay, schema and numerical logic.
If a backend does not emit completed `command_execution` entries into Codex
JSON, the safety gate fails closed with `INCOMPLETE` rather than fabricating
execution. Protocols without an explicit `--check` plus `run_log.json`
retain their existing generic gate behavior.

Focused WSL tests (no model calls):

    python -m pytest -q danus/tests/test_v8_adaptive.py danus/tests/test_v8_validation.py danus/tests/test_v8_review_convergence.py

The existing PFL3 project has a newer PENDING replacement proposal following
blocked task-237. It remains PENDING; normal Supervisor/Reviewer decisions
are required. Do not delete the old blocked task, rerun it blindly, restage
inputs, or manually edit `EVENTS.jsonl`.


### 2026-10-15: nested child exits and append-only revalidation

In the first real `validation_7` completion (task-253), the new Host
checker receipt returned `INCOMPLETE: run_log.json lacks numeric checker/parent
exit evidence`. This was a **Host parser mismatch**, not evidence of a
nonzero checker exit: the actual fresh run log stores child commands as
`children[].argv` containing `--replay` or `--check`, with corresponding
`children[].exit_code`. The gate now recognizes exactly one of each,
still rejects nonzero exits, requires a Host-owned completed entrypoint
execution trace, and preserves the original flat receipt behavior.

To avoid rerunning an immutable, already completed 256-episode validation
solely to correct a Host parser bug, `revalidate` is a narrow append-only
operation. It is allowed ONLY for the latest COMPLETE experiment whose current
Host validation is INCOMPLETE for `checker_execution`. Under the normal
V8 runner and store locks, it rechecks registered asset hashes and inputs,
reruns the approved host checks against unchanged fresh outputs, appends a NEW
`VALIDATION` event with `recheck_of_seq`, and publishes state. It does not
execute code in the Experimenter workspace, alter artifacts, modify the
original validation event, grant Reviewer scientific acceptance, or erase
prior OPEN issues. This is NOT a generic override of failed science.

Run the focused tests before using the command:

    python -m pytest -q danus/tests/test_v8_validation.py danus/tests/test_v8_adaptive.py danus/tests/test_v8_review_convergence.py

Then for the existing completed task:

    python -m danus.v8 revalidate v8-pfl3-steering-20261014 --task-id task-253

Inspect the returned result. A new PASSED Host receipt only establishes
approved mechanical checks and corroborated process exit; Reviewer must still
evaluate the independent replay implementation, scientific comparisons and
outstanding findings. If the receipt remains INCOMPLETE because the Codex
tool log lacks a matching command event, investigate that format without
rerunning production or inventing proof.


## 2026-10-15: strict host invocation and explicit checker contract

New Researcher proposals carry `check_contract=explicit_v1` as part of their
content-addressed identity. The Reviewer approves `required_checks` with an
explicit `checker_execution` item **only when the experiment requires an
executable checker**:

    {"id":"checker_execution","kind":"checker_execution",
     "path":"fresh/run.py","log_path":"fresh/run_log.json"}

Both paths must be safe workspace-relative, newly registered outputs. The
validation gate verifies recorded replay/checker exits and a matching Host-owned
completed command-execution event. It does **not** execute worker code in the
trusted host. The check is never enabled just because the new proposal text
mentions `--check` or `run_log.json`; omission of the typed check in a
protocol that needs it is a Reviewer approval defect, not independent proof.
At most one such typed check is allowed.

The Host now parses command tokens instead of finding `python` and a script
substring anywhere in an event. Only direct Python invocations of the exact
approved registered script (optionally preceded by a single `cd <workspace> &&`)
count. Quoted, printed, searched, piped, redirected or wrapped commands are
not execution receipts. Unknown command forms conservatively produce
`INCOMPLETE` rather than `PASSED`. This is a narrow proof of process
invocation and its exit code, not a proof of scientific checker correctness.

**History remains compatible**: approved proposals without
`check_contract=explicit_v1` use the previous `--check`/`run_log.json`
detection for their immutable contracts, but benefit from the stricter command
verification if Host revalidation is ever authorized. No historical event or
finished PFL3 task is edited. V8 experimental provenance, Reviewer authority
and science-vs-execution distinctions are unchanged.

Focused tests:

    python -m pytest -q danus/tests/test_v8_validation.py danus/tests/test_v8_adaptive.py danus/tests/test_v8_review_convergence.py danus/tests/test_v8_review_feedback.py danus/tests/test_v8_inputs.py



## 2026-10-15: exact prior-work handoff for explicit cross-role requests

Observed in the fly-CX compass pilot (task-3 -> task-7 -> task-11):
task-3 completed with a 5,195-character summary, well below the 32,000
character **hard** summary limit and the 22,400 character (70%) **recommended**
output budget. The model did not exceed its output budget. However,
`_worker` included only `summary[:3000]` in `recent_completed_work`, so
Reviewer saw a sentence cut in half and asked Researcher to reconstruct it.
That was a transfer/presentation defect, not missing original event evidence.

Fix: routine recent history is explicitly marked as a bounded
`summary_excerpt` with `excerpt_truncated` and `task_id`. When a task's
instruction, the incoming request's question, or that task's earlier
questions explicitly cites a completed `task-N`, the Host attaches the
**exact, full stored summary** in `referenced_completed_work` (up to three
completed cited tasks; any additional IDs are disclosed in
`omitted_referenced_task_ids`). The recipient is told to prefer that exact
record rather than treating previews as complete or reconstructing their
missing endings.

In addition, three normal role transitions receive **one** latest relevant
full summary even when the Supervisor instruction omits its ID: Researcher
work to early Reviewer work; completed Experimenter experiment to Researcher
interpretation; Researcher interpretation to final Reviewer review. This
avoids making ID wording a prerequisite for the ordinary review workflow.

Only the selected **completed summary** is exposed, not all task history,
raw artifacts or a universal workspace read permission. Supervisor routing
continues to use its short previews. The event ledger and previous projects
are not modified. This does not certify the summary's scientific claims:
ordinary Reviewer/source-evidence requirements still apply.

Focused offline regression: `danus/tests/test_v8_adaptive.py` simulates
a >4,900-character researcher summary, a reviewer requesting its missing
tail, and an answering researcher receiving the complete exact record in
the generated Worker prompt; unrelated long summaries stay bounded.


## 2026-10-15: targeted Supervisor evidence retrieval (fly-CX R2)

The R2 pilot demonstrated that the Worker handoff fix worked: task-7 consumed
the original task-3 summary without re-reading all CSVs. However, while
task-7 was WAITING for a Supervisor reply, Supervisor answered that only the
400-character routing preview was available, despite the exact summary being
present in the authoritative Store. This was not caused by the 70% output
budget or a failed Worker; it was a second missing **read path**, now in the
Supervisor rather than the Worker.

Supervisor continues to route with 400-character task previews. **Only when an
OPEN request is addressed to Supervisor**, `_brief_state` additionally
provides the full question and the **full completed task summary** explicitly
cited by `task-N`. When no task is cited, it supplies at most the latest
completed result by the requesting role, excluding the waiting task. It
never fabricates an unknown or incomplete source; it supplies details for at
most two Supervisor requests. If the source is unavailable, Supervisor must
route an answer task rather than inferring the missing tail.

The change neither expands normal Supervisor browsing into full histories nor
changes permissions, experiment approvals, immutable event records, or
scientific claims. The R2 pilot is not restarted; its pending proposal still
requires independent Reviewer approval. A local regression checks both the
short routing preview and the exact full source seen by the actual Supervisor
model prompt. Additional requests with no cited completed source remain
unresolved rather than fabricated.


## 2026-10-15: fly-CX R2 checker and revision handoff audit

After the second 8-step batch, fly-CX task-38 completed a Reviewer-approved
simulation+checker experiment, but Host checker_execution returned INCOMPLETE
because the nested-log parser insisted on a --replay child. The previous
PFL3 replay requirement does not apply to every new explicit checker.

Explicit checker_execution now mandates exactly one --check child. Optional
require_replay:true in the exact approved required_checks mandates one
--replay child as well; legacy prose-triggered receipts retain the stricter
old replay check for compatibility. All reported children must have numeric
zero exits, including simulator children; nonzero returns FAILED and missing
returns INCOMPLETE. A direct Host-owned completed entrypoint command remains
required. Host PASS only corroborates scope-limited execution evidence.

R2 also exposed an independently stored REVISE proposal whose full 12K text
was absent from Supervisor's short routing context and the corrective
Researcher's ordinary payload. Searching writable workspace is not the
way to retrieve authoritative frozen proposal text. Proposal IDs cited
in Supervisor requests or Worker assignments/questions now retrieve the
exact recorded proposal. Corrective Researcher work also receives the
latest REVISE contract, limited to that particular role and need. The
original proposal and approval history remain unchanged.

Run focused local regressions and inspect real task-38 run_log children
before using the append-only revalidate command; never rerun the 62MB
experiment only to correct a Host parser. New validation events preserve
the prior INCOMPLETE and do not certify scientific conclusions.


### 2026-10-15: actual task-38 receipt schema after append-only revalidation

The first follow-up revalidation remained INCOMPLETE after 79 WSL tests
passed: task-38's real run_log has children launched via simulate.py and
check.py, with numeric subprocess returncode fields rather than exit_code.
The parent also uses returncode. Moreover, Host-owned Codex logged a
single /bin/bash -lc 'python -B fresh/task20/run.py' command with exit 0.
Earlier synthetic tests exercised --check flags, exit_code fields and
direct Python commands, but not these actual formats.

The checker receipt now accepts numeric exit_code / returncode / return_code
with agreement when more than one is present, detects a directly executed
check.py or checker.py child as a checker as well as an explicit --check
flag, requires all logged child exits to be numeric zero and checks any
reported parent returncode. Explicit typed proposals need no replay unless
require_replay=true; older legacy-replay safeguards remain.

The Host execution receipt now understands one literal bash/sh -c/-lc
wrapper around a direct Python entrypoint, matching Codex's actual
command_execution event. It rejects echo, printf, chained commands, pipes,
redirections, shell substitutions and nested shell wrappers; unknown
forms fail closed to INCOMPLETE. The worker-authored run_log alone cannot
prove execution or independent checker science.

Added targeted offline regressions mimicking the real task-38 run_log
and shell-wrapped Codex host trace, including missing and nonzero child
and parent exits. No source results or old VALIDATION events are rewritten.
Rerun focused tests, inspect actual numeric returncode values, then use
append-only revalidate only if the recorded evidence supports it.


### 2026-10-15: checker file identity comes from approval, not a task filename

The generic typed checker_execution schema optionally pins checker_path
(a safe workspace-relative Python script path). When set, the host verifies
the exact approved script is the successful child recorded in run_log.json;
the script need not be named check.py or checker.py or accept --check.
An unrelated child, even with exit zero, does not satisfy that pinned check.
The approved checker_path is included in the content-addressed proposal.

Previously approved immutable checker contracts without checker_path retain
their established --check/check.py/checker.py interpretation and older
legacy replay compatibility. Host verification is deliberately limited to
approved Python wrapper commands (including a single literal sh/bash wrapper)
and documented exit evidence; it does not claim to support arbitrary shell
programming or to prove the independent scientific correctness of Checker.

An offline regression tests an arbitrary named verify_compass.py child
and a different child with the same successful exit code, which must remain
INCOMPLETE. Source task/project names are not part of the production logic.


## 2026-10-15: UCI air-quality pilot — autonomous completion and generic args/argv receipt

Third distinct pilot: v8-uci-co-drift-20261015, 10-step bounded run.
Completed 8 scientific tasks within the budget (9 decisions and STOP at step
10), 52 append-only events, FINISH at seq 52, Host latest PASSED,
independent final Reviewer ACCEPT, zero open requests/issues, no HALT.
Two approved experiments are recorded, but the second was strictly a
validation-receipt repair: it did not refit or rerun the frozen producer.
The scientific verdict is a bounded negative/uncertain finding: rolling
delayed CO interval calibration did not establish better weekly empirical
90% coverage stability than static calibration on this one dataset/split.
No universal drift-robustness or future-hour forecasting claim follows.

Real Host defect from first experiment task-11: producer and independent
verifier finished successfully, with all other 8 required checks passing,
but run_log.json stored subprocess argv as children[].args. Host scanner
recognized only children[].argv and therefore marked checker_execution
INCOMPLETE (seq 25). A diagnostic Researcher and independent Reviewer
identified the mismatch, then authorized a fresh checker-only experiment
task-34, which produced a compatible receipt and PASSED at seq 42.
This workaround preserved scientific evidence but spent avoidable roles
and new artifacts on Host formatting rather than scientific uncertainty.

Host now accepts command tokens from either children[].args or
children[].argv for its already-approved checker_path or old --check
contract. When both arrays exist and disagree, the receipt is FAILED
rather than silently preferring one. An empty/malformed command list
is INCOMPLETE; all child and parent exit evidence still must be numeric
zero and a separate Host-owned Codex command_execution event must
corroborate actual entrypoint invocation. There is no task-specific path
or UCI-specific condition in the production fix. Regressions cover both
formats, a wrong Checker name and conflicting arrays.

Guidance for further autonomy: first distinguish Host/parser INCOMPLETE
from a genuinely unexecuted/failed check. Prefer read-only diagnosis
and an append-only Host revalidation after a parser correction when the
evidence is already complete. A new Reviewer-approved checker-only run
remains permissible when required evidence genuinely does not exist;
never bypass the science Reviewer or promote unverified claims.

The completed pilot remains immutable. Local regression pytest and
a READ-ONLY dry-run of task-11's original frozen validation contract
against its registered outputs are requested before trusting the fix
in another pilot. Do not reopen or rerun the FINISHed study.


### Residual architecture question: latest validation versus source experiment

An independent observation from the UCI completed ledger: Host FINISH gates
currently inspect latest_validation and latest Experimenter COMPLETE, while
the original scientific experiment task-11 still retains its prior
checker-only INCOMPLETE event; task-34 is a second, approved checker-only
validation experiment with PASSED. In this specific pilot the independent
final Reviewer explicitly inspected and reconstructed task-11's frozen
producer, evaluation and both checker receipts, so the final bounded
conclusion is not invalidated by the historical receipt-parser bug.

The Host does NOT yet provide a general machine-enforced producer-to-repair
lineage that proves a later checker-only PASSED settles the original source
experiment. For future multi-experiment projects that distinction matters.
A blanket requirement that every historical incomplete experiment become
PASSED would also be wrong: superseded or genuinely failed trials must
remain truthful historical evidence. Defer lineage design until a narrow
failing case makes necessary semantics explicit; do not re-open this
completed pilot or silently change past validation events.
