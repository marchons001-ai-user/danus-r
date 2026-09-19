import pytest
import fcntl
import multiprocessing

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.gateway import server


@pytest.mark.parametrize("operation", ["handoff", "contract", "plan"])
def test_rebound_worker_cannot_submit_under_replacement_authority(tmp_path, monkeypatch, operation):
    ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    old = ledger.assign(worker="r", role="researcher", node_id="root", plan_version=1, task="old")
    current = ledger.assign(worker="r", role="researcher", node_id="root", plan_version=1, task="new")
    monkeypatch.setenv("DANUS_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("DANUS_AUTHOR", "r")
    monkeypatch.setenv("DANUS_ROLE", "researcher")
    monkeypatch.setenv("DANUS_ASSIGNMENT_ID", old["assignment_id"])
    before = ledger.path.read_bytes()
    with pytest.raises(ValueError, match="assignment"):
        if operation == "handoff":
            server.handoff_request("ESCALATE", "old round request")
        elif operation == "contract":
            server.implementation_submit_contract("root", 1, "old goal", ["keep"], ["avoid"], ["check"])
        else:
            server.plan_propose_change(1, "old proposal", [{"op": "add_node", "node_id": "x", "title": "X", "parent": "root"}])
    assert ledger.path.read_bytes() == before
    assert ResearchPlanGraph(tmp_path).pending_changes() == []
    assert ledger.current("r")["assignment_id"] == current["assignment_id"]


def _paused_handoff_write(root, connection):
    original = server.HandoffRequests.open

    def open_(self, **kwargs):
        connection.send("validated")
        assert connection.recv() == "release"
        return original(self, **kwargs)

    server.HandoffRequests.open = open_
    try:
        server.handoff_request("ESCALATE", "bounded request")
        connection.send("written")
    finally:
        connection.close()


def test_gateway_holds_assignment_lock_until_handoff_write(tmp_path, monkeypatch):
    ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    current = ledger.assign(worker="r", role="researcher", node_id="root", plan_version=1, task="task")
    for key, value in {"DANUS_PROJECT_DIR": str(tmp_path), "DANUS_AUTHOR": "r", "DANUS_ROLE": "researcher", "DANUS_ASSIGNMENT_ID": current["assignment_id"]}.items():
        monkeypatch.setenv(key, value)
    ctx = multiprocessing.get_context("fork")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_paused_handoff_write, args=(tmp_path, child))
    process.start()
    try:
        assert parent.poll(10)
        assert parent.recv() == "validated"
        with (tmp_path / "progress" / ".assign.lock").open("a+b") as handle:
            with pytest.raises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        parent.send("release")
        assert parent.poll(10)
        assert parent.recv() == "written"
        process.join(10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
        process.join(10)
        parent.close()
        child.close()


@pytest.mark.parametrize("invalid", ["missing_id", "done", "project_pause", "worker_pause", "reopen"])
def test_worker_writes_require_live_unpaused_authority(tmp_path, monkeypatch, invalid):
    ResearchPlanGraph.create(tmp_path, node_id="root", title="Root")
    ledger = ProgressLedger(tmp_path)
    current = ledger.assign(worker="r", role="researcher", node_id="root", plan_version=1, task="task")
    for key, value in {"DANUS_PROJECT_DIR": str(tmp_path), "DANUS_AUTHOR": "r", "DANUS_ROLE": "researcher", "DANUS_ASSIGNMENT_ID": current["assignment_id"]}.items():
        monkeypatch.setenv(key, value)
    if invalid == "missing_id":
        monkeypatch.delenv("DANUS_ASSIGNMENT_ID")
    elif invalid == "done":
        ledger.update(worker="r", state="DONE", actor="runtime", outcome="COMPLETE")
    elif invalid == "reopen":
        ledger.set_node_state("root", state="OPEN", actor="operator")
    else:
        directory = tmp_path if invalid == "project_pause" else tmp_path / "workers" / "r"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".pause").write_text("manual\n", encoding="utf-8")
    with pytest.raises(ValueError, match="assignment"):
        server.handoff_request("ESCALATE", "must reject")
    assert server.HandoffRequests(ledger).all() == []
