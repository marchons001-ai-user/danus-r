import json
import subprocess
import sys

import pytest

from danus.core._util import read_jsonl
from danus.orchestration.tests.test_audit_r8_assignment_task_identity import _worker


@pytest.mark.parametrize("phase", ["before_task", "after_task", "after_stale", "after_assigned"])
def test_92_assignment_intent_recovers_exactly_once_in_fresh_process(tmp_path, monkeypatch, phase):
    root, worker, ledger, old = _worker(tmp_path, monkeypatch)
    code = '''
import os, sys
from pathlib import Path
from danus.core import progress
from danus.orchestration import cli
phase = sys.argv[1]
original_replace = Path.replace
original_append = progress.append_jsonl
def replace(path, target):
    if Path(target).name == 'TASK.md' and phase == 'before_task': os._exit(73)
    value = original_replace(path, target)
    if Path(target).name == 'TASK.md' and phase == 'after_task': os._exit(73)
    return value
def append(path, event):
    original_append(path, event)
    if event['event'] == 'STATE' and phase == 'after_stale': os._exit(73)
    if event['event'] == 'ASSIGNED' and phase == 'after_assigned': os._exit(73)
Path.replace = replace
progress.append_jsonl = append
cli.do_assign('P/researcher', 'new exact task', node_id='root')
'''
    assert subprocess.run([sys.executable, "-c", code, phase], timeout=15).returncode == 73
    intent_path = root / "progress/.assignment.json"
    intent = json.loads(intent_path.read_text())
    recovery = "from pathlib import Path; from danus.orchestration.cli import _recover_project; import sys; _recover_project(Path(sys.argv[1]))"
    for _ in range(2):
        assert subprocess.run([sys.executable, "-c", recovery, str(root)], timeout=15).returncode == 0
    assert not intent_path.exists()
    current = ledger.current("researcher")
    assert current["assignment_id"] == intent["event"]["assignment_id"] != old["assignment_id"]
    assert current["state"] == "ASSIGNED"
    assert current["task_sha256"] == ledger.task_sha256((worker / "TASK.md").read_text())
    events = read_jsonl(ledger.path)
    assert events.count(intent["event"]) == 1
    assert all(events.count(event) == 1 for event in intent["superseded"])


def test_92_corrupt_intent_fails_closed_before_task_or_authority_write(tmp_path, monkeypatch):
    root, worker, ledger, old = _worker(tmp_path, monkeypatch)
    before = ledger.path.read_bytes(), (worker / "TASK.md").read_bytes()
    (root / "progress/.assignment.json").write_text(json.dumps({"intent_id": "bad", "task": "forged"}))
    with pytest.raises(ValueError, match="corrupt assignment intent"):
        with ledger.assignment_lock():
            pytest.fail("corrupt intent must not grant authority")
    assert before == (ledger.path.read_bytes(), (worker / "TASK.md").read_bytes())
