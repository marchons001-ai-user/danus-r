# E1 numeric-runtime preflight block: controlled recovery and visible intervention

Project: `fly-open10m-20261009`
Date: 2026-10-10 (Asia/Shanghai)
Status: TUI blocker visibility and modal dismissal implemented; 45s Watchdog default implemented; opt-in isolated numerical venv scripts implemented. Autonomous Reviewer-approved environment provisioning is NOT implemented.

## Observed evidence

- Plan v3, R3 and V2 completed; Implementation Contract `2d4ac25321c730a0` APPROVE.
- E1 Experimenter assignment `bfbccf2deb162839` is BLOCKED, before execution.
- Preflight `runtime/workspaces/fly-open10m-20261009/reports/E1_environment_preflight.md` reports missing NumPy and a supported autodiff framework in `/usr/bin/python3` and `runtime/venv/bin/python`.
- Reviewer assignment `24ddfb441d110657` independently confirms missing packages and opened main escalation `938c50954f42f39c` (OPEN).
- Global-idle Watchdog detected both episodes; retry to reviewer E1 was rejected after an already completed reviewer diagnosis. Changing 180s to 45s alone cannot provision runtime prerequisites.
- Operator reports **six older frozen protocols** whose `execution_runtime.executable` equals the shared `runtime/venv/bin/python`. Installing into that venv risks invalidating `danus.core.runtime.validate()` because runtime identity hashes installed wheel RECORDs.
- WSL currently exposes ~7.4 GiB RAM, 2 GiB swap and an RTX 4060 8 GiB GPU. These are observations, not proof the approved matrix fits the resource budget.

## Current code authority

- `danus/gateway/roles.py`: Reviewer can approve scientific Implementation Contracts; Experimenter can call `experiment_run`. Neither has a governed environment provisioning tool.
- `agents/contracts/experimenter.md`: scientific APPROVE is not a license to change scientific method, skip environment gates, or invoke the frozen runtime via shell.
- `danus/core/evidence.py` freezes `execution_runtime`; `danus/core/runtime.py` binds Python executable, venv config and installed distribution manifests to the protocol.
- `danus/execution/experiment.py` validates runtime identity and executes frozen units using `sys.executable`; merely creating another venv from inside E1 does **not** redirect execution to it.
- OPEN `ESCALATE target_role=main` captures the request, but no existing autonomous host provisioner consumes this class of escalation.

## Target governance flow

1. **E1 preflight** produces a structured `ENVIRONMENT_REQUEST`, naming concrete package versions / index requirements, selected CPU-or-CUDA backend, Python version, allocation ceiling and why the environment is necessary under the already-approved experiment.
2. **Reviewer** determines whether proposed dependencies and compute constraints preserve the scientific contract; Reviewer `APPROVE` authorizes *only that semantic environment request*. It cannot grant unrestricted installation or mutate the host.
3. **Trusted Supervisor provisioning service** checks an operator-configured deployment policy: permitted package indexes and pinned artifacts, disk/CPU/RAM/GPU limits, network access solely during provisioning, and disallowance of privileged system mutation. Provision into a **project-specific isolated venv**; never install into the shared `runtime/venv`.
4. On success it persists `ENV_READY` with interpreter absolute path, Python/pip/kernel/backend versions, full dependency lock and hashes, resource allocation, request/review IDs, and a validated import + autodiff smoke test. Failures persist as `ENV_FAILED` with actionable reason and a safe retry policy.
5. **Trusted execution runtime** binds the selected immutable environment both when freezing `execution_runtime` and when executing the frozen bundle. Today both use the Gateway/control venv's `sys.executable`; they must be refactored *together*, preserving the existing exact-assignment, protocol-ID pinning, sandbox, checkpoint and evidence validation. A dynamically chosen interpreter may not be passed from an untrusted worker.
6. **Supervisor recovery** revalidates the environment and approved contract, records the resolved Main escalation, and retries E1 at most once under the same frozen scientific task. Subsequent failures must become a new non-duplicate incident, not an endless model retry.
7. **Operator involvement** is the exceptional outcome for disallowed package/index, privileged permission, unavailable resources, unrecoverable network issue, or material scientific change. TUI must surface the original failure, owning role, open escalation and exact next prerequisite.

## Non-goals / invariants

- Reviewer approval does not itself authorize arbitrary `pip install`.
- No self-modification of `runtime/venv` or other project environments.
- No unreviewed changes to seeds, matrix, metrics, dataset, baseline, model family or budget.
- No hidden re-freeze after observing results; frozen protocol and artifact hashes are never silently overwritten.
- No blanket permission for network calls from sandboxed experimental units.
- No E1 retry based only on elapsed time while the stated environment prerequisite remains unsatisfied.
- Legacy frozen protocols keep their original interpreter identity, unchanged.

## Required acceptance checks before deploying provisioning

- Six legacy `a2-8e-native0160*` protocol runtime identities still validate exactly after numerical environment provisioning.
- E1 `ENV_REQUESTED -> REVIEWED -> ENV_READY` remains auditable and deterministic; rejected requests do not execute commands.
- Exactly the approved package set and interpreter used for smoke tests also determine `runtime.identity()` at protocol freeze and `runtime.validate()` at execution.
- Network disabled during frozen units, no ambient host write access, and allowed resource limits honored.
- Interrupted install returns recoverable ENV_FAILED and never publishes a partially provisioned environment as ready.
- The Watchdog never duplicates a completed Reviewer E1 clarification, and operators see the pending host prerequisite immediately rather than waiting for another deadlock timeout.
- TUI read-only snapshot is unchanged by viewing blockers, and the `b` dialogue can explain a BLOCKED assignment with an OPEN Main escalation.

## Interim safe handling

Keep E1 BLOCKED; do not mutate the shared runtime. Use the TUI blocker panel to inspect the persisted environment preflight and escalation. The separately proposed 45s Watchdog threshold is unrelated to environment readiness. Provisioning/retry should wait for the reviewed, controlled runtime capability above. This document is an implementation contract for control-plane work, **not** a claim that provisioning is already supported.

## Interim implementation in `fix/tui-close-idle45-20261010`

- `b` opens a read-only Blocker dialogue. `Esc`, `b`, `q`, and the visible Close button dismiss it; the banner disappears when the current blocked Assignment is superseded by a non-BLOCKED Assignment.
- `DANUS_DEADLOCK_IDLE_SECONDS` defaults to 45, applies only to eligible **all-idle** incidents, and does not bypass deterministic recovery guards or provision dependencies.
- `scripts/provision_project_science_env.sh <project> [cuda|cpu]` is an **operator-run** opt-in script that installs a pinned numerical stack outside shared `runtime/venv`; it aborts if any frozen protocol already binds to the target interpreter. It records an environment identity and pip lock.
- `scripts/run_project_science_research.sh <project> [budget]` launches the Supervisor and spawned workers/Gateways under that isolated Python interpreter; `bin/danus research` would instead use the shared venv unless deliberately configured otherwise.
- The operator must stop the earlier Supervisor, provision and validate the project environment, use that interpreter for exactly one governed E1 recovery, and only then start the replacement Supervisor. The historical reviewer `ESCALATE` remains auditable; there is no implicit approval or silent cancellation.
- These host scripts are an interim workaround. They are **not** an Agent-exposed installer, and no new automatic environment Request/Review/Provision gateway has been implemented. Future governed automation must enforce the full sequence and tests above before allowing unattended provisioning.


## Reusing an existing WSL environment without reinstalling PyTorch

The opt-in project launcher now accepts an absolute existing WSL Python interpreter as its third argument, or `DANUS_SCIENCE_PY`. Its preflight imports NumPy, SciPy, PyTorch, Danus Gateway/orchestration, OpenAI and Anthropic and verifies a minimal autodiff gradient. It does not invoke pip. Run `danus.core.runtime.identity()` on the candidate environment **before** recovering E1: a Conda package lacking a wheel `RECORD` may fail Danus's strict frozen runtime manifest check. If this happens, do not work around runtime validation or silently mutate frozen environments. Investigate controlled opt-in setup or a separate environment design.

`nvidia-smi` proves WSL GPU-driver visibility, not that a Python torch wheel is installed. Inspect candidate WSL Python environments first, then choose an already compatible interpreter. Keep the shared `runtime/venv` unchanged, and never run a second Supervisor simultaneously.
