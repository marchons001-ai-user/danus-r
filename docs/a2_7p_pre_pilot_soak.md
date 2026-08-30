# A2-7p Pre-Pilot Long-Run Integration Soak

Purpose: exercise the existing Danus-R control plane as one integrated system before a real research pilot. This stage does not add production behavior. It uses one deterministic scientific falsification/follow-up loop to expose orchestration failures that isolated tests may miss.

## Fixed fixture

Workers:

- `low`: Researcher, GPT-5.6 Terra, low effort.
- `low2`: Reviewer, `BACKEND=auto` (DSH primary, Terra only after explicit DSH quota exhaustion).
- `low3`: Experimenter, GPT-5.6 Terra, low effort.

Scientific fixture: one deterministic smooth signal plus Gaussian observation noise. Baseline is raw-current persistence; treatment is a five-sample trailing moving average. E1 freezes five noise levels: 0.01, 0.05, 0.10, 0.20, 0.40. The universal hypothesis is intentionally falsifiable. If E1 falsifies it, the Researcher must propose a post-hoc Plan revision adding only a boundary-localization follow-up.

Initial Plan route:

`R1 -> V1 -> E1 -> V2 -> R2 -> V3`

An approved result-conditioned revision must add exactly:

`V3 -> E2 -> V4 -> R3`

E2 reuses the same fixture at sigma 0.14, 0.16, 0.18, 0.20, 0.22. No new method, dependency, tuning, or expanded matrix is allowed.

## Behaviors under observation

Required integration observations:

- one assignment is consumed once; no duplicate start or duplicate experiment execution;
- dependencies are not crossed and ready-but-unassigned nodes remain `pending_assignment` rather than receiving invented tasks;
- worker runtime, Progress Ledger, Plan version, Evidence and Supervisor decisions converge to one consistent state;
- a completed worker does not cause automatic scientific node closure; Supervisor/operator completion remains explicit;
- the E1 Experimenter assignment is rejected unless it exactly matches the Reviewer-approved Implementation Contract;
- Researcher proposes, Reviewer reviews, and only control-plane authority commits the post-hoc Plan revision;
- E1/E2 use immutable protocols, checkpointed units and immutable Evidence Records; negative cells remain present;
- `proxy=estimated` quota telemetry never causes a hard Codex stop; DSH Reviewer is not gated by Codex quota;
- Codex stderr noise such as model-list refresh timeouts is non-fatal unless forward progress stops;
- the expected missing `optional_input.csv` is attempted once only; repeated identical missing-file retries are a failure signal;
- no unexplained repeated tool calls, wait/no-op loops, orphan worker, stale assignment reuse, or uncontrolled extra rounds;
- Reviewer must be willing to reject the universal hypothesis when raw evidence falsifies it rather than rationalizing the result.

## Deliberate recovery observation

After E1 reaches assignment `DONE` but before node E1 is closed, stop the `danus supervise --watch` process with Ctrl-C and restart the same watcher. This is a control-plane restart only; do not delete runtime files. E1 must remain DONE, its Evidence Record must remain unique, and no worker may rerun E1 merely because the watcher restarted.

Do not inject additional worker crashes during the first baseline soak. If the baseline passes, a separate recovery subtest may later force-stop an active worker; do not mix that failure with the first integrated run.

## Stop conditions

PASS only after:

1. R1, V1, E1, V2, R2 and V3 complete in dependency order.
2. The one post-hoc Plan change is Reviewer-approved and control-plane committed.
3. E2, V4 and R3 complete in dependency order under the new plan version.
4. Root P0 is explicitly closed.
5. All workers are idle, there are no pending Plan changes, no live experiment executor, and the Supervisor reports no remaining frontier/retask/review action.
6. E1 and E2 each have exactly one Evidence Record with complete matrix coverage and no duplicated completed unit.

Stop immediately and preserve logs/state if any of these appear:

- same assignment starts or executes twice;
- a downstream node starts before dependencies are DONE;
- Plan/Progress version mismatch or unexpected STALE assignment;
- semantic gate can be bypassed or an approved contract hash does not bind the Experimenter task;
- repeated identical deterministic tool failure with no new information;
- worker is alive but produces no new event/state while repeatedly retrying/waiting;
- Supervisor restart causes duplicate work, lost state, or orphan processes;
- Reviewer approves a materially altered contract/Plan proposal or ignores contradicting raw evidence;
- experiment artifacts/checkpoint/evidence hashes disagree;
- quota `estimated` state pauses Codex, or Codex quota pauses a DSH worker.

On failure, do not delete or recreate the project. Stop the watcher/workers as needed, keep the project directory and workspace intact, and inspect the smallest relevant logs/state before changing code. A2-7p fixes only defects reproduced by the soak; it does not add speculative architecture or convenience features.
