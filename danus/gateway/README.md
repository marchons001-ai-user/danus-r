# danus/gateway — role-gated MCP server (the permission gate)

The **only sanctioned door to the truth stores.** A stdio MCP server (`danus-core`)
whose exposed tools depend on the caller's role — permission is enforced by *which
tools a role can even see*, not by prompt convention.

```
danus/gateway/
  server.py            the 6 MCP tools + the scientific fact_submit write-gate
  roles.py             ROLE_TOOLS — the role→tools table (the security surface)
  __main__.py          `python -m danus.gateway` → build_app().run() (role from DANUS_ROLE)
  tests/test_gateway.py
  tests/test_scientific_gate.py
```

## The role table (`roles.py`)

| role | tools |
|---|---|
| worker | `gm_add gm_search fact_submit fact_search search_arxiv_theorems` |
| main | `gm_add gm_search fact_search fact_revoke search_arxiv_theorems` (**no `fact_submit`**) |
| verifier | `search_arxiv_theorems` only (read-only) |

Ungated tools are **physically absent** from the surface. Unknown, mis-typed, or
*unset* role → **fail-closed** to the verifier set; the full dev set requires the
explicit `DANUS_ROLE=all`.

## The single write-gate (`fact_submit`, in `server.py`)

Both verified fact types enter through the same MCP tool:

- `fact_type="analytical"` (default) keeps the historical `statement + proof`
  contract and calls `DANUS_VERIFY_URL` (normally `/verify`). Existing callers do
  not need to change.
- `fact_type="empirical"` requires an explicit `scope` and one or more immutable
  `evidence_refs`; it calls the sibling `/verify/empirical` route. A prose proof is
  neither required nor used for empirical evidence.

The gate performs only cheap input checks itself. The verify service owns the
verification routing: mathematical prechecks + Math Verifier for analytical facts;
deterministic evidence integrity checks + semantic evidence audit for empirical
facts.

For either fact type the gate then:

1. writes the fact to the **same Scientific Fact Graph** iff the verifier verdict is
   `correct`;
2. if `source_id` was supplied and the graph write actually succeeded, appends a
   `verified` status/back-link to that Global Memory finding;
3. always records a `verification` trace once a verifier verdict exists, including
   type, scope/evidence refs where relevant, write failures, promotion failures,
   and the verifier report.

A `wrong` verifier verdict does **not** automatically mark the source finding
`refuted`: insufficient/invalid support is not evidence that the scientific claim
is false. Service unreachable / malformed response → clean `verdict="error"`, no
fact write and no fabricated verifier verdict.

`DANUS_VERIFY_URL` remains one configuration value. If it ends in `/verify`, the
empirical route is derived as `/verify/empirical`; otherwise the gateway appends
`/verify/empirical`.

## Launched by

`bin/danus-mcp` (role=main, for Claude Code via `.mcp.json`); each worker's
`.codex/config.toml` (role=worker); the mathematical verify launcher injects it
(role=verifier) so that judge can call `search_arxiv_theorems`. Config
(`DANUS_PROJECT_DIR`, `DANUS_AGENTS_ROOT`, `DANUS_VERIFY_URL`, role, author) is read
at **call time**.

## Pinned interfaces

The 6-tool set + role table; `python -m danus.gateway` launch; the verify HTTP seam;
the default analytical `fact_submit(statement, proof, ...)` behavior.

## Tests

All gateway tests are offline: verifier calls are mocked or use a local stdlib HTTP
server. No Codex/DSH/LLM is launched.

```bash
python -m danus.gateway.tests.test_gateway
python -m danus.gateway.tests.test_scientific_gate
```
