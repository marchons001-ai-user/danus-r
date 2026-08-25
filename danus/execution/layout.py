"""On-disk layout, project/worker resolution, and role parsing for the swarm.

This is the single source of truth for *where everything lives* — shared by
``danus.execution`` (the loop + scaffolding) and ``danus.orchestration`` (the CLI
verbs). Every path/name is derived here so the two halves can never drift.

Legacy mathematical workers use ``worker.md`` + ``agents/skills/worker``.
Scientific workers use the small ``scientific_worker.md`` spine plus their injected
role contract and Research Charter; they have no default skill bundle in A2-5b.

Everything reads env at CALL time (not import time) to match core/gateway/verify.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

# --------------------------------------------------------------------------- #
# per-worker / per-project control-file names                                  #
# --------------------------------------------------------------------------- #

TASK_FILE = "TASK.md"
ROLE_FILE = ".role"
PID_FILE = ".pid"
LOCK_FILE = ".pid.lock"
STOP_FILE = ".stop"
STATUS_FILE = ".status.json"
LOGS_DIR = "logs"
DEADLINE_FILE = ".run_deadline"
RESEARCH_CHARTER_FILE = "RESEARCH_CHARTER.md"

SCIENTIFIC_ROLES = frozenset({"researcher", "reviewer", "experimenter"})


# --------------------------------------------------------------------------- #
# roots (env read at call time)                                               #
# --------------------------------------------------------------------------- #

def repo_root() -> Path:
    """The repo root that holds the ``agents/`` tree (contracts + skills)."""
    return Path(__file__).resolve().parents[2]


def agents_root() -> Path:
    """Where projects live. Override with ``DANUS_AGENTS_ROOT``; defaults to
    ``runtime/projects`` under the current working directory."""
    env = os.environ.get("DANUS_AGENTS_ROOT")
    if env:
        return Path(env).resolve()
    return (Path.cwd() / "runtime" / "projects").resolve()


def worker_md() -> Path:
    """Legacy mathematical worker contract. Override with
    ``DANUS_WORKER_CONTRACT``."""
    env = os.environ.get("DANUS_WORKER_CONTRACT")
    if env:
        return Path(env).resolve()
    return repo_root() / "agents" / "contracts" / "worker.md"


def worker_skills_dir() -> Path:
    """Legacy mathematical worker skills. Override with
    ``DANUS_WORKER_SKILLS``."""
    env = os.environ.get("DANUS_WORKER_SKILLS")
    if env:
        return Path(env).resolve()
    return repo_root() / "agents" / "skills" / "worker"


def scientific_worker_md() -> Path:
    """Minimal common contract for Researcher/Reviewer/Experimenter."""
    return repo_root() / "agents" / "contracts" / "scientific_worker.md"


def scientific_role_md(role: str) -> Optional[Path]:
    """Return the supplementary Danus-R contract for a scientific worker role.

    Unknown/legacy roles intentionally return ``None`` so existing math workers
    keep their historical behavior. The allow-list also prevents a role value from
    becoming an arbitrary filesystem path."""
    name = (role or "").strip().lower()
    if name not in SCIENTIFIC_ROLES:
        return None
    return repo_root() / "agents" / "contracts" / f"{name}.md"


def harness_contract(role: str) -> Path:
    """Standing AGENTS.md contract for ``role``."""
    return scientific_worker_md() if (role or "").strip().lower() in SCIENTIFIC_ROLES else worker_md()


def harness_skills_dir(role: str) -> Optional[Path]:
    """Default skill bundle for ``role``; scientific roles start with none."""
    if (role or "").strip().lower() in SCIENTIFIC_ROLES:
        return None
    return worker_skills_dir()


def research_charter_template() -> Path:
    return repo_root() / "agents" / "contracts" / "research_charter.md"


# --------------------------------------------------------------------------- #
# project / worker dirs                                                       #
# --------------------------------------------------------------------------- #

def project_dir(project: str) -> Path:
    return agents_root() / project


def research_charter(project: str) -> Path:
    return project_dir(project) / RESEARCH_CHARTER_FILE


def workers_dir(project: str) -> Path:
    return project_dir(project) / "workers"


def worker_dir(project: str, worker: str) -> Path:
    return workers_dir(project) / worker


def list_workers(project: str) -> List[str]:
    wd = workers_dir(project)
    if not wd.is_dir():
        return []
    return sorted(p.name for p in wd.iterdir() if p.is_dir())


def list_projects() -> List[str]:
    """Every project under the agents root (a dir holding a ``workers/`` subdir)."""
    root = agents_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "workers").is_dir())


# --------------------------------------------------------------------------- #
# typed per-worker layout                                                     #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class WorkerLayout:
    """Typed view of one worker home. ``dir`` is the codex cwd / LocalMemory root;
    every control file is a property so callers never re-spell the names."""

    dir: Path

    @property
    def name(self) -> str:
        return self.dir.name

    @property
    def project_dir(self) -> Path:
        # <project>/workers/<worker>  ->  <project>
        return self.dir.parents[1]

    @property
    def project(self) -> str:
        return self.project_dir.name

    @property
    def task(self) -> Path:
        return self.dir / TASK_FILE

    @property
    def role(self) -> Path:
        return self.dir / ROLE_FILE

    @property
    def pid(self) -> Path:
        return self.dir / PID_FILE

    @property
    def lock(self) -> Path:
        return self.dir / LOCK_FILE

    @property
    def stop(self) -> Path:
        return self.dir / STOP_FILE

    @property
    def status(self) -> Path:
        return self.dir / STATUS_FILE

    @property
    def logs(self) -> Path:
        return self.dir / LOGS_DIR

    @property
    def local_memory(self) -> Path:
        return self.dir / "local_memory"

    @property
    def codex_config(self) -> Path:
        return self.dir / ".codex" / "config.toml"


# --------------------------------------------------------------------------- #
# target parsing                                                             #
# --------------------------------------------------------------------------- #

def resolve_target(target: str) -> Tuple[str, Optional[str]]:
    """``"proj"`` -> (proj, None);  ``"proj/worker"`` -> (proj, worker)."""
    target = target.strip().strip("/")
    if "/" in target:
        project, worker = target.split("/", 1)
        return project, (worker or None)
    return target, None


def target_worker_dirs(target: str) -> List[Path]:
    """Worker dirs addressed by ``target`` — one (proj/worker) or all (proj)."""
    project, worker = resolve_target(target)
    if worker:
        return [worker_dir(project, worker)]
    return [worker_dir(project, w) for w in list_workers(project)]


# --------------------------------------------------------------------------- #
# role spec                                                                  #
# --------------------------------------------------------------------------- #

_ROLE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*?):(\d+)$")


def parse_roles(spec: str) -> List[Tuple[str, str]]:
    """``"high:3,xhigh:4"`` -> ``[("high","high"), ("high2","high"),
    ("high3","high"), ("xhigh","xhigh"), …]``. Returns (worker_name, base_role)
    pairs; the base role (digits stripped) drives the codex reasoning effort. The
    first worker of a base keeps the bare name; the rest get numeric suffixes.
    Raises ``ValueError`` on a malformed/empty spec or a count < 1.

    Historical note: this CLI field is an effort roster, not the Danus-R
    scientific ``ROLE``. The scientific role lives independently in each worker's
    ``.role`` file."""
    out: List[Tuple[str, str]] = []
    for part in (p.strip() for p in spec.split(",") if p.strip()):
        m = _ROLE_RE.match(part)
        if not m:
            raise ValueError(f"bad role spec {part!r}; want e.g. high:3,xhigh:4")
        base, count = m.group(1), int(m.group(2))
        if count < 1:
            raise ValueError(f"role count must be >= 1: {part!r}")
        for i in range(1, count + 1):
            name = base if i == 1 else f"{base}{i}"
            out.append((name, base))
    if not out:
        raise ValueError("empty role spec")
    return out
