# danus/core — the Danus data model (library)

Pure-Python, protocol-agnostic. **Read [`DATA_MODEL.md`](DATA_MODEL.md) first** —
the detailed legacy model; A2-4 extends its truth layer additively rather than
replacing the existing mathematical path.

**Code only touches fixed data structures.** Everything behavioral — when to
publish a finding, when to verify, which verifier to use, when to promote, and
how to orchestrate experiments — remains prose / later workflow code.

```
danus/core/
  DATA_MODEL.md      detailed original model
  local_memory.py    LocalMemory   — per-worker private, rough recall log
  global_memory.py   GlobalMemory  — project-shared, strongly typed findings
  evidence.py        EvidenceStore — frozen protocols + empirical evidence records
  factgraph.py       FactGraph     — one shared verified scientific-fact DAG
  schema.py          Fact + analytical/empirical content-addressed ids
  bm25.py            BM25 recall
  _util.py           append-only JSONL helpers
  tests/              offline deterministic tests
```

## Four project knowledge layers

```
local memory   private process log                     not truth
global memory  shared hypotheses/findings/dead ends    not truth
evidence       frozen protocol + what actually ran     provenance
a fact graph   verified scientific claims              truth source
```

The fact graph is **one DAG**, not separate theory/experiment graphs. Facts are
classified by support path:

- `analytical` — existing statement + proof facts; historical ids and serialization
  remain backward compatible.
- `empirical` — statement + explicit tested `scope` + immutable `evidence_refs`.

A counterexample is not a third fact type: it can be analytical or empirical.
Experiment protocols are evidence/provenance records, not facts.

## EvidenceStore

`EvidenceStore(project_dir)` is intentionally small and file-first:

```
<project>/evidence/protocols/<protocol_id>.json
<project>/evidence/records/<evidence_id>.json
```

`freeze_protocol(...)` writes an immutable approved plan. A changed plan becomes
a new protocol with `parent_protocol_id`; result-dependent changes are explicitly
marked `post_hoc=true` with a `revision_reason`. A2-4a only records that lineage;
later workflow code will determine the flag from run state.

`add_record(...)` records what actually ran: protocol id, commit/data/config refs,
matrix coverage, primary metrics, failures, deviations, and artifact references.
It does **not** decide whether the result supports a scientific claim.

`artifact_ref(path)` computes `{path, sha256, size}` without copying the artifact.
Large experiment outputs therefore stay in their existing location while later
verification can detect mutation deterministically.

## API

```python
from danus.core import EvidenceStore, FactGraph, GlobalMemory, LocalMemory

# private rough process memory
lm = LocalMemory(worker_dir)
lm.append("notes", {"thought": "..."})

# shared findings / hypotheses / dead ends
gm = GlobalMemory(project_dir)
gid = gm.append("counterexample", claim="...", evidence="...", author="worker")

# frozen empirical protocol + actual run record
ev = EvidenceStore(project_dir)
protocol_id = ev.freeze_protocol(
    question="Does UQ track prediction error?",
    hypothesis="Higher UQ is associated with higher error.",
    matrix={"noise": [0.0, 0.1]},
    primary_metrics=["spearman_rho"],
    decision_rule="support if rho > 0.3",
    author="researcher",
)
evidence_id = ev.add_record(
    protocol_id=protocol_id,
    author="experimenter",
    code_commit="abc123",
    data_ref="heldout-v1",
    matrix_coverage={"completed": 2, "planned": 2},
    primary_metrics={"spearman_rho": 0.61},
)

# analytical fact — historical path unchanged
fg = FactGraph(project_dir)
analytical_id = fg.add(
    problem_id="P", author="researcher", statement="...", proof="...",
    predecessors=[], glossary_introduces={},
)

# empirical fact — same DAG, different support payload
empirical_id = fg.add_empirical(
    problem_id="P",
    author="researcher",
    statement="On heldout-v1, UQ is positively associated with prediction error.",
    scope={"dataset": "heldout-v1", "model": "abc123"},
    evidence_refs=[evidence_id],
    evidence_summary="Frozen protocol; complete matrix; rho=0.61.",
    predecessors=[],
)
```

`predecessors` always means **fact dependency**: if predecessor A is revoked, a
fact depending on A is cascade-revoked. Protocols, CSVs, figures, and run records
are never predecessor nodes; empirical support is referenced through
`evidence_refs`.

## Mechanical invariants implemented in A2-4a

- Existing analytical `compute_fact_id(...)` is unchanged.
- Empirical ids bind statement + tested scope + evidence-record ids + fact
  predecessors.
- Empirical facts require non-empty scope and at least one evidence reference.
- Frozen protocol revisions never overwrite their parent; post-hoc revisions are
  explicit and reasoned.
- Evidence records are content-addressed and reference artifacts by SHA-256/size.
- Analytical and empirical facts share search, predecessor DAG, and cascade revoke.
- No verifier, LLM, scheduler, experiment runner, or new dependency is added here.

A2-4b will add verification routing. Until then, `FactGraph.add_empirical(...)` is
**data-structure I/O only**, exactly like `FactGraph.add(...)`: callers must not
interpret its existence as verification unless the later gate has accepted it.

## Test

```bash
python -m danus.core.tests.test_core
python -m danus.core.tests.test_scientific_schema
```
