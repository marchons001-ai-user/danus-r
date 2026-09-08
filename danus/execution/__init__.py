"""danus.execution — the per-worker autonomous outer loop + on-disk layout.

The self-driving round loop (``loop``), the project/worker scaffolding + detached
launch (``scaffold``), and the canonical on-disk layout every other module reads
(``layout``). ``danus.orchestration`` (the CLI verbs) is the thin UX shell over
this library.

Run one worker's loop directly with ``python -m danus.execution <worker_dir>``
(this is how ``danus start`` launches it, detached).
"""

from __future__ import annotations

import importlib

from . import layout
from .layout import (
    DEADLINE_FILE,
    LOCK_FILE,
    LOGS_DIR,
    PID_FILE,
    ROLE_FILE,
    STATUS_FILE,
    STOP_FILE,
    TASK_FILE,
    WorkerLayout,
    agents_root,
    list_projects,
    list_workers,
    parse_roles,
    project_dir,
    repo_root,
    resolve_target,
    target_worker_dirs,
    worker_dir,
    worker_md,
    worker_skills_dir,
    workers_dir,
)

_LAZY_EXPORTS = {
    "kickoff": "loop",
    "main": "loop",
    "run_round": "loop",
    "write_status": "loop",
    "atomic_write": "scaffold",
    "do_new": "scaffold",
    "spawn_loop": "scaffold",
    "symlink": "scaffold",
}


def __getattr__(name: str):
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    module = importlib.import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value

__all__ = [
    # layout module + API
    "layout",
    "WorkerLayout",
    "agents_root",
    "repo_root",
    "project_dir",
    "workers_dir",
    "worker_dir",
    "list_workers",
    "list_projects",
    "worker_md",
    "worker_skills_dir",
    "resolve_target",
    "target_worker_dirs",
    "parse_roles",
    # control-file name constants
    "TASK_FILE",
    "ROLE_FILE",
    "PID_FILE",
    "LOCK_FILE",
    "STOP_FILE",
    "STATUS_FILE",
    "LOGS_DIR",
    "DEADLINE_FILE",
    # loop
    "main",
    "run_round",
    "write_status",
    "kickoff",
    # scaffolding
    "do_new",
    "spawn_loop",
    "atomic_write",
    "symlink",
]
