---
name: scientific-performance
description: Optimize approved scientific computations when runtime, memory, CPU/GPU utilization, or experiment throughput is materially limiting. Measure first, preserve scientific semantics, reuse existing optimized code/libraries, and stop when further optimization is not decision-relevant.
---

# Scientific Performance

Use this skill only when compute performance is materially limiting an approved experiment. The objective is lower wall time or resource cost **without changing the scientific protocol or result meaning**.

## Non-negotiable constraints

Performance work must not silently change:

- experiment cells, seeds, sample count, horizon, resolution, data split, exclusions, or stopping rules;
- model/controller equations, solver tolerances, convergence criteria, or primary metrics;
- numerical dtype/precision, deterministic behavior, or random-number semantics when those can affect the approved result;
- raw-artifact or checkpoint requirements.

Do not install a new performance dependency merely because it may be faster. Reuse the repository, standard library, platform tools, and already-installed dependencies first. If a new dependency or a scientifically meaningful numerical change is genuinely required, stop and request approval.

## Procedure

### 1. Avoid work before accelerating it

Before optimizing code, check in this order:

1. Can an existing artifact answer the question without rerunning anything?
2. Is the same computation already implemented or cached in the repository?
3. Can the approved computation be reduced algorithmically without changing its semantics?
4. Can repeated setup, parsing, model loading, compilation, or preprocessing be reused across units?

Do not optimize computation that should have been eliminated.

### 2. Measure the real bottleneck

Run the smallest representative pilot that preserves the relevant execution path. Record a baseline wall time and, when useful, memory and device utilization.

Prefer tools already available on the machine, for example:

- Python `time.perf_counter()` / `cProfile`;
- `/usr/bin/time -v`;
- `lscpu`, `free -h`, `nvidia-smi`;
- profiler/benchmark utilities already provided by the active numerical framework.

Separate compute, I/O, startup/setup, synchronization, and data-transfer costs when that distinction changes the optimization decision. Do not guess the bottleneck from source appearance alone.

### 3. Optimize in descending leverage

Use the first sufficient option; do not jump directly to custom CUDA or low-level tuning.

1. **Algorithm/work reduction** — remove repeated work, use a better exact algorithm/data structure, reuse invariant quantities.
2. **Batching/vectorization** — replace Python-level scalar loops with existing NumPy/PyTorch/JAX/etc. operations when the project already uses them.
3. **Existing optimized library path** — use an already-installed BLAS, framework primitive, compiled implementation, or repository fast path.
4. **Bounded CPU parallelism** — parallelize independent approved units when CPU-bound and memory permits.
5. **GPU execution** — use the GPU only for sufficiently large, parallel, compute-heavy work where transfer/startup overhead does not dominate.
6. **Low-level kernels/SIMD/JIT** — only after profiling proves higher-level approaches insufficient.

Prefer one high-impact change over several speculative micro-optimizations.

## CPU rules

- For independent CPU-bound Python work, prefer bounded processes unless existing compiled code already releases the GIL or provides its own threading.
- For I/O-bound work, threads may be appropriate.
- Avoid nested parallelism: do not combine a large process pool with unrestricted BLAS/OpenMP/framework threads.
- Choose concurrency from measured CPU, RAM, and per-unit memory pressure; never use every visible core by default merely because it exists.
- Preserve deterministic per-unit seeds independently of scheduling order.

## GPU/CUDA rules

Use GPU acceleration when the workload has enough parallel work and data reuse to amortize launch and host-device transfer costs.

- Prefer existing framework operations and batching before custom kernels.
- Keep data resident on device across related operations when practical; avoid repeated host-device copies and forced synchronization.
- Size batches with memory headroom instead of running at the edge of OOM.
- Avoid multiple competing GPU processes unless measurements show it helps and memory is safe.
- Synchronize appropriately when benchmarking asynchronous GPU work; otherwise timing is invalid.
- Do not switch `float64` to `float32`, enable mixed precision, `fastmath`, approximate solvers, nondeterministic kernels, or relaxed tolerances solely for speed unless the approved scientific protocol permits it and equivalence is verified.

## Experiment-matrix parallelism

Parallelism may change scheduling, never scientific scope.

- Execute only already-approved units; do not add cells because spare CPU/GPU capacity exists.
- Respect Experiment Runtime checkpoint/safe-point semantics and existing artifact integrity checks.
- Do not rerun completed units just to improve machine utilization.
- Bound concurrency so RAM, VRAM, disk bandwidth, and thermals do not destabilize other units.
- If resource contention changes timing-sensitive behavior or numerical results, reduce concurrency rather than accepting contaminated evidence.

## Verification after every accepted optimization

Compare the optimized path with the pre-optimization reference on a representative deterministic case before scaling out.

Use the strongest cheap check that matches the scientific semantics:

- exact/bitwise equality when expected;
- an already-approved numerical tolerance when floating-point order may differ;
- identical primary metrics, matrix coverage, seeds, and artifact schema;
- deterministic regression tests already present in the repository.

A faster result that cannot be shown equivalent is not an optimization; it is a method change and requires review.

## Benchmark discipline

For meaningful timing comparisons:

- compare on the same machine/environment and representative input;
- separate one-time warm-up/compilation from steady-state timing when relevant;
- use repeated measurements when noise can change the conclusion;
- report wall-time improvement and any material memory/VRAM trade-off;
- stop when the remaining bottleneck is outside the approved task or further complexity is not justified by experiment wall time.

Do not create a permanent benchmark framework, report format, configuration layer, or tuning subsystem just for one experiment.

## Handoff

Include only performance facts needed to reproduce the execution decision:

- measured baseline and bottleneck;
- optimization actually kept;
- target hardware/runtime assumptions that matter;
- measured speedup/resource change;
- correctness/equivalence check used and result;
- any remaining resource limitation.

Then use Ponytail before handoff to remove exploratory optimization residue. Keep no superseded fast paths, debug instrumentation, or unused tuning knobs.