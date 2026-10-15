# Offline V8 gate demonstration

Run from the repository root after installing Danus-R (no Codex login,
remote API keys, network access, datasets or third-party scientific packages):

```bash
python scripts/demo_v8_offline.py
```

The command creates a temporary V8 Store with deliberately **synthetic**
two-row measurements (`mse` = 1, 3) and a reported mean of 2.0. It runs
**scripted** role decisions through the real V8 Host APIs, then deletes the
workspace on exit:

1. Researcher submits a fixed protocol containing three executable Host
   checks: registered artifact integrity, CSV row count, and independent
   recomputation of the reported numeric mean.
2. Reviewer approves the exact immutable proposal.
3. Experimenter registers the synthetic outputs; the real Host verifier
   independently checks the approved conditions and returns `PASSED`.
4. An attempted `FINISH` **before** final review is rejected by the
   actual deterministic Supervisor gate.
5. Researcher completes a scripted interpretation, Reviewer records the
   bounded `INCONCLUSIVE` verdict, and Supervisor now records `FINISH`.

Output is compact JSON showing the event kinds, role sequence, three Host
check results, rejected premature completion and final state. A regression
test is in `danus/tests/test_v8_offline_demo.py`.

**What this does NOT establish:** It is NOT an autonomous agent run.
No model reasoned about a research question, no new experiment was scientifically
performed, no external reviewer audited real-world data, and a passed Host
integrity gate is not a claim of scientific truth. The demo demonstrates
mechanics, permissions and replayable transition records only.

For exploratory real-agent case outcomes with their important limitations,
see [development notes](DEVELOPMENT_NOTES.md) and the
[V8 architecture](V8_ADAPTIVE.md).
