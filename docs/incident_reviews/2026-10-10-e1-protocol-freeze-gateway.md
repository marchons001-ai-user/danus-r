# E1 second blocker: missing authorized protocol freeze interface

Date: 2026-10-10 (Asia/Shanghai)
Project: `fly-open10m-20261009`
Plan: v3, R3/V2 done and Implementation Contract `2d4ac25321c730a0` independently APPROVE.

## Evidence and exact failure

Experimenter E1 retry Assignment `00b3caac58212834` verified that its bound numerical interpreter imports NumPy, SciPy and PyTorch with CUDA. It returned BLOCKED before implementation/experiment: there was no approved protocol-freezing interface exposed to the restricted Worker. Its Main escalation is `0311ca97b02b489a` (OPEN). Earlier Reviewer `938c50954f42f39c` still describes the *prior* missing numerical environment and must not be displayed as if it came from this new Experimenter attempt.

## Root cause

`EvidenceStore.freeze_protocol` was implemented as a trusted Python API, and `experiment_run` already accepts a frozen protocol linked to a reviewed Implementation Contract, with exact Assignment, reviewer, content-hash and immutable bundle checks. But `agents/contracts/experimenter.md` and the round prompt told Experimenter to call that API directly from its shell. The Codex worker sandbox intentionally hides project control files and the Evidence Store, and the Experimenter Gateway tools included `experiment_run` but no protocol-freezing verb. Therefore that documented action was not available in the legal execution context.

## Narrow fix

Add `experiment_freeze_protocol` to **only** the Experimenter Gateway tool set. It:
- Requires the bound live Experimenter Assignment, exact task identity, independently approved Implementation Contract and RESOLVED review handoff.
- Rejects CLARIFY and exact-protocol-ID assignments.
- Accepts the approved scientific question, hypothesis, finite matrix, primary metrics, decision rule, named reducer, and explicit source files.
- Resolves files **only** in the project's separate scientific workspace. Rejects absolute/traversal/symlink inputs, verifies model-provided hashes if present, and creates content-addressed references for inputs/reducer.
- Invokes the existing `EvidenceStore.freeze_protocol` with the current Contract and Assignment IDs and exact source bundle, then validates the returned identity through `_approved_dynamic_protocol(..., pin=False)`.
- Leaves actual execution to the existing `experiment_run(protocol_id)`, which independently authorizes and pins the first attempted protocol. Never changes Plan or self-approves a contract.
- Performs slow file/whole-environment hashing outside Supervisor project locks and rechecks capability after freezing.

The Experimenter contract and trusted round prompt now direct the Worker to use that exposed tool, not a control-side Python call. The TUI displays the current E1 escalation rather than an older Reviewer escalation with the same node ID.

## Recovery and tests

New test `danus/gateway/tests/test_governed_protocol_freeze.py` checks exact reviewed assignment, protocol/bundle IDs and stable replay, role gating, path traversal, symlink, incorrect input hash, stale capability and unbound access; `danus/tui/tests/test_blocker_explain.py` checks current-escalation linkage and a protocol-specific next step. Tests are committed but need execution in the user WSL environment before retrying E1.

Stop Supervisor, merge, run focused tests, then perform **one** governed recovery of E1 using the *same bound numerical interpreter*. Keep both historical escalations in the append-only audit log; do not delete or backdate approvals. If a later blocker appears, stop and inspect it instead of repeatedly dispatching blindly.
