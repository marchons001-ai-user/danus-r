# S5-D0 — Sandbox execution contract

Status: S5-D0 acceptance contract. S5-D1 uses this contract for empirical
reducer verification.

## Decision

Danus-R will reuse the official Linux Codex sandbox as the isolation engine. Do
not fork Codex, vendor bubblewrap, or add another sandbox dependency unless a
measured requirement cannot be expressed by the official implementation.

S5-D0 deliberately did **not** add a `SandboxAdapter` class. The first
production caller now uses the smallest function/module that satisfies this
contract at `danus/execution/sandbox.py`.

## Minimal future interface

The first implementation only needs semantics equivalent to:

```python
run_sandboxed(
    command,
    *,
    cwd,
    readable_paths=(),
    writable_paths=(),
    network=False,
    env=None,
    timeout=None,
)
```

Do not add role/profile classes, backend registries, resource-limit objects, or
fallback implementations until a real caller needs them. Worker/reducer/
experiment policy belongs at the call site until duplication proves a smaller
shared representation is useful.

## Load-bearing invariants

1. **Official engine.** Linux isolation is provided by `codex sandbox`; Danus
   does not reimplement namespaces/bubblewrap.
2. **Fail closed.** Missing/unsupported Codex sandbox, invalid policy, or launch
   failure must fail the requested operation. There is no automatic fallback to
   ordinary `subprocess.run`.
3. **Deny by default.** Start from filesystem root denied, then add only the
   minimum runtime reads plus explicitly declared readable/writable paths.
4. **Resolved executable only.** The real Codex executable path (`readlink -f`
   equivalent) may be made read-only so the standalone launcher can re-exec.
   Never expose all of `~/.codex` merely to make the sandbox start.
5. **Explicit path authority.** Canonicalize host paths before policy creation.
   Writable authority is never inferred from a readable parent. Symlinks do not
   grant authority to their targets.
6. **Clean environment.** Sandboxed scientific code starts from an allowlisted
   environment (`env -i` semantics). Parent secrets are not inherited unless a
   future call site explicitly proves they are required.
7. **Network off by default.** Reducer and experiment execution must start with
   direct network disabled. Worker networking must not be enabled merely to make
   Gateway IPC work.
8. **Timeout is a boundary.** A future production wrapper must terminate the
   sandbox process tree on timeout. CPU/RAM/cgroup quotas are S5-D4 hardening,
   not part of this phase.
9. **Gateway is outside this contract.** Codex 0.153.4 blocks Unix-socket connect
   when network is disabled, and precise single-socket permission was observed
   to be unreliable. S5-D2 must therefore design Gateway IPC independently
   (preferred direction: pre-opened/inherited file descriptor), rather than
   weakening the filesystem/network sandbox.

## Required policies at the first callers

### Worker (#96)

- own scientific workspace: read/write;
- required task/input material: read-only;
- Plan, Progress, FactGraph, PAUSE and other workers' private state: not readable
  and not writable;
- truth/control mutation: only through the external Gateway path.

### Reducer (#95)

- reducer, protocol and evidence artifacts: read-only;
- result scratch: read/write;
- network: off;
- environment: explicit allowlist, no verifier secrets;
- reducer Python must never execute in the long-lived verifier process.

### Experiment (#87)

- frozen execution bundle and declared inputs: read-only;
- artifact/output scratch: read/write;
- live scientific workspace: absent unless itself represented by an immutable
  declared input;
- network: off unless the protocol explicitly gains a future reviewed network
  capability.

Sandboxing does not replace content freezing. #87 still requires a content-hash
manifest / frozen execution bundle before the experiment caller can be trusted.

## S5-D0 acceptance

`scripts/probe_codex_sandbox.py` is the executable acceptance probe. It uses only
Python's standard library plus an installed official Linux Codex CLI and must be
run on the same Linux/WSL host that will execute Danus workers.

The probe must demonstrate:

- workspace read/write succeeds;
- an explicitly readable input is readable but not writable;
- fake `plan/`, `progress/`, `fact_graph/`, `PAUSE`, and another worker's private
  memory cannot be read/modified/deleted;
- absolute-path, `..`, and symlink traversal do not escape the denied tree;
- a nested subprocess inherits the restriction;
- a parent secret is absent from the sandbox;
- loopback connection to a host listener fails with network disabled.

A PASS means only that the **official sandbox engine and the S5-D0 policy shape**
work on that host. It does not close #87/#95/#96 until their production call
sites are migrated and separately regression-tested.

## Local acceptance command

```bash
cd ~/danus-r
python3 scripts/probe_codex_sandbox.py
```

Optional explicit binary:

```bash
python3 scripts/probe_codex_sandbox.py --codex /home/user/.local/bin/codex
```

The probe is intentionally outside pytest because it depends on host WSL/Linux
sandbox capabilities. The normal unit suite must remain runnable on machines
without Codex installed.
