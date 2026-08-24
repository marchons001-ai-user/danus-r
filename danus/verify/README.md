# danus.verify — Scientific Verification Layer

`danus.verify` is the HTTP verification service behind Danus-R's fact write gate.
It now has two deliberately separate verification paths while preserving the
historical mathematical contract.

## Routes

### Analytical / mathematical

```text
POST /verify
{"statement": "...", "proof": "..."}
```

The existing path is unchanged:

```text
statement + proof
→ deterministic math prechecks
→ cold-start mathematical verifier (`agents/contracts/verifier.md`)
→ verdict: correct | wrong
```

The mathematical verifier is an informal LLM judge, not Lean or another formal
proof assistant. Existing `fact_submit` callers continue to use this route.

### Empirical

```text
POST /verify/empirical
{
  "statement": "...",
  "project_dir": "/path/to/project",
  "scope": {...},
  "evidence_refs": ["<evidence_id>", ...],
  "evidence_summary": "..."
}
```

The empirical path is intentionally ordered by cost and determinism:

```text
candidate empirical claim
→ deterministic Evidence Store checks
   - project/scope/evidence references
   - content-addressed evidence-record identity
   - frozen protocol identity + revision/post-hoc lineage
   - explicit matrix coverage
   - artifact existence, byte size, SHA-256
→ if any deterministic check fails: verdict=wrong, NO LLM CALL
→ otherwise: semantic evidence audit (`agents/contracts/empirical_verifier.md`)
→ verdict: correct | wrong
```

The semantic verifier judges only questions a deterministic program cannot
reliably decide from generic experiment records: whether the claim is actually
supported, whether it exceeds the tested scope, whether it hides negative/failed
runs or deviations, whether a post-hoc result is being misrepresented as
independent confirmation, and whether causal/mechanistic language exceeds the
experimental design.

`post_hoc=true` is not an automatic rejection. It is provenance: the claim must be
phrased and interpreted accordingly.

The empirical semantic verifier receives the small protocol/evidence manifests,
not raw experiment trees. It has no Danus MCP tools, does not search the web, and
does not re-run experiments. Raw artifacts are protected by the hashes stored in
the Evidence Store.

## Why there are two paths

The distinction is by **verification mechanism**, not by separate truth stores.
Analytical and empirical facts later enter the same Scientific Fact Graph; only
the evidence and gate differ.

- analytical fact → proof correctness;
- empirical fact → evidence integrity + semantic claim/evidence support.

A program should decide every condition it can decide exactly before an LLM is
used. This both lowers cost and avoids asking a probabilistic judge to verify file
integrity or arithmetic bookkeeping.

## HTTP behavior

`GET /health` returns:

```json
{"status": "ok", "pid": 12345}
```

For both verification paths, `wrong` is a normal scientific verdict and is
returned as HTTP 200. Transport/process failures remain HTTP errors.

The empirical deterministic-rejection response includes the failed checks in
`verification_report.deterministic_checks`, so the caller can repair provenance
without spending a semantic-verifier call.

## Launcher behavior

Both semantic agents reuse `danus.verify.launcher` for:

- unique run ids;
- isolated run directories;
- model/effort resolution through `danus.codex`;
- timeout/error handling;
- log capture;
- reading `verification.json`.

The analytical verifier retains its existing Danus theorem-search MCP tool and
verification skills. The empirical verifier uses a separate agent home and no MCP
tools/skills because deterministic work is completed before it runs.

## Configuration

Shared/analytical settings remain compatible:

- `VERIFY_HOST` / `VERIFY_PORT`
- `VERIFY_AGENT_HOME`
- `VERIFIER_RESULTS_DIR`
- `DANUS_VERIFY_MODEL` / `DANUS_VERIFY_EFFORT`
- `DANUS_CODEX_MODEL` / `DANUS_CODEX_EFFORT`
- `CODEX_TIMEOUT_SECONDS`
- existing mathematical precheck toggles

Empirical semantic-audit overrides are optional:

- `EMPIRICAL_VERIFY_AGENT_HOME`
- `DANUS_EMPIRICAL_VERIFY_MODEL`
- `DANUS_EMPIRICAL_VERIFY_EFFORT`

If empirical model/effort overrides are unset they fall back to the analytical
verify overrides, then to the neutral Codex defaults. This allows a cheaper
semantic-audit model to be selected without weakening deterministic checks.

## Current A2-4 boundary

A2-4b provides verification routing only. The gateway's existing `fact_submit`
still uses the analytical route. A2-4c will connect empirical submission to this
route, write accepted empirical facts into the unified Scientific Fact Graph, and
persist the final verification/promotion trace.

The verify service itself is stateless with respect to project truth stores: it
reads empirical Evidence Store records for verification but does not write the
Fact Graph or Global Memory.
