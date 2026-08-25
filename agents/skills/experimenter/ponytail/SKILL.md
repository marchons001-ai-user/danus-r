---
name: ponytail
description: Keep Experimenter code changes minimal and clean. Use after implementation or repair, before handoff, to remove exploratory residue without broadening scope.
---

# Ponytail

Use this skill for the final cleanup pass on code changed by the Experimenter. The goal is a diff that contains only the implementation that is actually needed now, not the history of how it was discovered.

## Procedure

1. Confirm the current implementation satisfies the approved task and its tests/checks.
2. Inspect only the touched code and directly related new files.
3. Remove residue that is no longer required by the final behavior:
   - failed or superseded implementation attempts;
   - unused variables, functions, branches, flags, imports, files, or fallback paths;
   - duplicated logic introduced during repair;
   - temporary compatibility or debug scaffolding that the task does not require;
   - comments that narrate attempts, mistakes, or earlier versions.
4. Keep comments only when they explain a non-obvious reason the final code must remain as written.
5. Re-run the smallest deterministic checks that cover the cleanup.
6. Stop. Do not use cleanup as permission to refactor unrelated code, generalize an interface, add abstractions, or expand the experiment.

## Output criterion

The final diff should read as if the correct implementation had been written directly: minimal, internally consistent, and free of abandoned attempts. If removing something would change required behavior or cannot be validated cheaply, keep it and report the uncertainty instead of guessing.
