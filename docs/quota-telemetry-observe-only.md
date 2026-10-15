# Removal of predictive Codex quota gating

Danus-R no longer models a **synthetic five-hour quota** or projects the
recent five-hour burn forward to a weekly reset. The local calibration,
model-weighted credit estimates, `PRESSURE` state and
`block_new_codex_rounds`-driven suspension have been removed.

## What remains

- The official Codex `account/rateLimits/read` snapshot is informational.
  A window appears **only when the server actually reports it**.
  TUI shows the reported weekly remainder with `OBSERVE_ONLY`, never a local
  prediction. Telemetry refresh cannot stop, pause, resume or re-route a worker.
- Explicit Danus `--token-budget`, project deadline, per-round runtime
  timeout and user-directed PAUSE/STOP/RESUME remain in force.
- Genuine backend errors, authentication errors and provider-enforced limits
  are still surfaced by the model client. Removing a predictive policy does
  **not** grant an unlimited service quota.
- Existing `BACKEND=auto` Reviewer keeps using isolated Codex; independently
  verified DSH backend exhaustion handling is not the speculative Plus
  five-hour proxy and remains unchanged.

## One-time runtime migration

On the first *applied* Supervisor reconciliation for an existing project,
`_clear_legacy_quota_pauses` removes **only** Worker pause markers beginning
with `quota:`. It removes the old companion `.stop` marker if that worker is
already dead. It does not touch `manual`, `budget:` or `review:` pauses,
nor any other project's state. Existing RUNNING Assignments can then be
restarted through normal Supervisor reconciliation; no Plan nodes or
Completion records are manually advanced.

The migration is idempotent and reports `legacy_pause_cleanup`.
For a running `bin/danus research` process, restart the controlling command
after checking out this branch so the Python process loads the new code.
Preserve the original trace/logs for auditing. Resuming a previously interrupted
run is a functional continuation, **not** an uninterrupted fresh autonomy
acceptance test.

## Focused verification

`python -m pytest -q danus/orchestration/tests/test_quota_policy.py
danus/orchestration/tests/test_quota_telemetry.py
danus/orchestration/tests/test_reviewer_cost_policy.py`

A later broader regression is required before merging the refactor into main.
