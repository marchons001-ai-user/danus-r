# Contributing to Danus-R

Danus-R is an experimental derivative of
[FrenzyMath/Danus](https://github.com/frenzymath/Danus), not an official
upstream release. Please preserve the Apache-2.0 license and original
authorship notices; identify substantial modifications to inherited files.

## What a useful PR includes

- One bounded problem, motivation, and the smallest relevant code change.
- A deterministic regression test when changing an authorization,
  experiment approval, evidence-reading, validation or FINISH invariant.
- Evidence from the exact code revision under test. Never weaken the Host
  checker, remove failing tests, or assume model prose equals verified fact.
- A clear distinction between **reproducible code** and **scientific
  conclusions**. Exploratory, inconclusive and negative results are valid.
- A note explaining whether existing immutable proposals or event histories
  are affected. Never silently rewrite stored evidence or retroactively
  declare a historical failure PASSED.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest -q danus/tests/test_v8_validation.py danus/tests/test_v8_adaptive.py danus/tests/test_v8_offline_demo.py
python scripts/demo_v8_offline.py
```

CI separates offline V8 checks from the full repository suite with the
official Codex sandbox. Tests that require model-provider credentials must
not run unprotected against pull requests from untrusted forks.

No real API keys, private datasets, raw Codex sessions, workstation paths
or unredacted security reports belong in public issues or commits. For a
security-sensitive defect use [SECURITY.md](SECURITY.md), not a public issue.

Historical upstream mathematics features are described in
[ARCHITECTURE.md](ARCHITECTURE.md), while the V8 research workflow is
described in [docs/V8_ADAPTIVE.md](docs/V8_ADAPTIVE.md).
