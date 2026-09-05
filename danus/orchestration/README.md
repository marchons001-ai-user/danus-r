# danus/orchestration — the `danus` CLI verbs

The operator's lifecycle commands. **Verbs/UX only** — the on-disk layout, the
round loop, and scaffolding live in `danus/execution`; this module parses arguments
and calls into it. Run via `bin/danus`.

```
danus/orchestration/
  cli.py        the verbs: list / new / assign / finalize / start / status / supervise / pause / resume / stop
  __main__.py   `python -m danus.orchestration` (what bin/danus execs)
```

## Verbs

| verb | does |
|---|---|
| `list [--json]` | projects + live worker counts + model |
| `new <p> [--roles high:3,xhigh:4] [--model M]` | → `execution.scaffold.do_new` |
| `assign <p>/<w> (--task/--file/--stdin)` | overwrite that worker's `TASK.md` |
| `finalize <p> [<fact_id>…]` | record target(s) in `TARGET.md` (no id ⇒ suggest terminal facts); records only, does not stop workers |
| `start <p>[/<w>]` | → `execution.scaffold.spawn_loop` (idempotent via `.pid.lock`) |
| `status <p>[/<w>] [--json]` | per-worker liveness + round + progress/context state + `stuck?` soft signal |
| `supervise <p> [--apply]` | deterministic Plan/Progress/liveness/resource reconcile; `--apply` executes process actions |
| `supervise <p> --watch` | change-driven long-running Supervisor; default 30-minute heartbeat fallback |
| `pause <p>[/<w>]` | persist `.pause` and stop the addressed worker(s) |
| `resume <p>[/<w>]` | clear `.pause` and restart the addressed worker(s) |
| `stop <p>[/<w>] [--force]` | graceful `.stop` / `--force` `killpg` |

## Notes

- Liveness is **zombie-aware** (`os.kill(pid,0)` + a `/proc/<pid>/stat` Z-state
  check), so `status`/`list` don't lie and `start` can restart a crashed worker.
- `supervise --watch` polls only small control/liveness state and reconciles on a
  change; unchanged projects use the coarse heartbeat. Deterministic paths do not
  run an LLM. Only `SCIENTIFIC_ROUTE_REQUIRED` may invoke the restricted Luna/low
  router, and its durable decision is replayed instead of calling Luna again.
- Per-session context budgets/rollover remain in `danus.execution.context`; frozen
  experiment `matrix.units` remains the experiment execution budget. Supervisor
  observes both rather than duplicating either mechanism.
- Touches core only indirectly: `new` creates the empty `global_memory/`/`fact_graph/`
  dirs (populated lazily by core on first write); it never writes the truth stores.

## Tests

`python -m pytest danus/orchestration/` (offline; fake codex + stub project).
