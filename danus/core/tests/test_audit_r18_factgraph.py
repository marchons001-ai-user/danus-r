import multiprocessing
import subprocess
import sys

import pytest

from danus.core import FactGraph


def _add(graph, empirical=False, predecessors=None, statement="A"):
    args = dict(problem_id="P", author="r", statement=statement, predecessors=predecessors)
    if empirical:
        return graph.add_empirical(**args, scope={"dataset": "fixed"}, evidence_refs=["e1"], evidence_summary="complete evidence")
    return graph.add(**args, proof="complete proof")


@pytest.mark.parametrize("empirical", [False, True])
def test_88_predecessors_must_be_active(tmp_path, empirical):
    graph = FactGraph(tmp_path)
    with pytest.raises(ValueError, match="predecessor_missing"):
        _add(graph, empirical, ["0123456789abcdef"])
    parent = _add(graph)
    child = _add(graph, empirical, [parent], "B")
    assert graph.exists(child)
    graph.revoke(parent, "reject")
    with pytest.raises(ValueError, match="predecessor_revoked"):
        _add(graph, empirical, [parent], "C")
    assert not graph.exists(child)


@pytest.mark.parametrize("empirical", [False, True])
def test_89_revocation_cannot_be_replayed_as_add(tmp_path, empirical):
    graph = FactGraph(tmp_path)
    fid = _add(graph, empirical)
    graph.revoke(fid, "operator rejected")
    with pytest.raises(ValueError, match="fact_revoked"):
        _add(graph, empirical)
    assert not graph.exists(fid)
    assert graph.list() == []
    assert (graph.revoked_dir / f"{fid}.md").is_file()


def test_89_revocation_log_also_prevents_resurrection(tmp_path):
    graph = FactGraph(tmp_path)
    fid = _add(graph)
    graph.revoke(fid, "operator rejected")
    (graph.revoked_dir / f"{fid}.md").unlink()
    with pytest.raises(ValueError, match="fact_revoked"):
        _add(graph)
    assert not graph.exists(fid)


def _adding(root, parent, pipe, empirical):
    graph = FactGraph(root)
    original = graph._check_predecessors
    def check(ids):
        original(ids)
        pipe.send("checked")
        assert pipe.recv() == "release"
    graph._check_predecessors = check
    pipe.send(_add(graph, empirical, [parent], "child"))


def _revoking(root, parent, pipe):
    pipe.send("started")
    pipe.send(FactGraph(root).revoke(parent, "reject"))


@pytest.mark.parametrize("empirical", [False, True])
def test_90_add_and_revoke_linearize_across_processes(tmp_path, empirical):
    graph = FactGraph(tmp_path)
    parent = _add(graph)
    ctx = multiprocessing.get_context("fork")
    a, ac = ctx.Pipe()
    b, bc = ctx.Pipe()
    add = ctx.Process(target=_adding, args=(tmp_path, parent, ac, empirical))
    revoke = ctx.Process(target=_revoking, args=(tmp_path, parent, bc))
    try:
        add.start()
        assert a.poll(10) and a.recv() == "checked"
        revoke.start()
        assert b.poll(10) and b.recv() == "started"
        assert not b.poll(0.3)
        a.send("release")
        assert a.poll(10)
        child = a.recv()
        assert b.poll(10) and set(b.recv()) == {parent, child}
        add.join(10)
        revoke.join(10)
        assert add.exitcode == revoke.exitcode == 0
        assert graph.list() == []
    finally:
        for proc in (add, revoke):
            if proc.is_alive():
                proc.kill()
            if proc.pid:
                proc.join(10)
        for pipe in (a, ac, b, bc):
            pipe.close()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("cut", [1, 50, 180])
@pytest.mark.parametrize("empirical", [False, True])
def test_94_killed_writer_never_publishes_partial_truth(tmp_path, existing, cut, empirical):
    graph = FactGraph(tmp_path)
    fid = _add(graph, empirical) if existing else None
    before = graph.get_raw(fid) if fid else None
    code = '''
import os, sys
from pathlib import Path
from danus.core.tests.test_audit_r18_factgraph import _add
from danus.core import FactGraph
original = Path.open
class Partial:
    def __init__(self, handle): self.handle = handle
    def __enter__(self): return self
    def __exit__(self, *args): self.handle.close()
    def write(self, data):
        self.handle.write(data[:int(sys.argv[2])])
        self.handle.flush()
        os.fsync(self.handle.fileno())
        os._exit(73)
def opening(path, *args, **kwargs):
    handle = original(path, *args, **kwargs)
    return Partial(handle) if path.name.endswith('.md.tmp') else handle
Path.open = opening
_add(FactGraph(Path(sys.argv[1])), sys.argv[3] == 'True')
'''
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path), str(cut), str(empirical)], timeout=15)
    assert result.returncode == (0 if existing else 73)
    if existing:
        assert graph.get_raw(fid) == before
    else:
        assert graph.list() == []
        assert graph.exists(_add(graph, empirical))


@pytest.mark.parametrize("empirical", [False, True])
def test_94_corruption_fails_all_truth_reads(tmp_path, empirical):
    graph = FactGraph(tmp_path)
    fid = _add(graph, empirical)
    path = graph._path(fid)
    path.write_text(path.read_text()[:-10] + "\n")
    for read in (lambda: graph.exists(fid), graph.list, lambda: graph.get_raw(fid), lambda: graph.search("A")):
        with pytest.raises(ValueError, match="corrupt fact"):
            read()


def test_94_revoke_crash_keeps_entire_closure_inactive(tmp_path):
    graph = FactGraph(tmp_path)
    parent = _add(graph)
    child = _add(graph, predecessors=[parent], statement="child")
    code = '''
import os, sys
from pathlib import Path
from danus.core import FactGraph
original = os.replace
def replace(src, dst):
    original(src, dst)
    if Path(dst).parent.name == '_revoked': os._exit(73)
os.replace = replace
FactGraph(Path(sys.argv[1])).revoke(sys.argv[2], 'reject')
'''
    assert subprocess.run([sys.executable, "-c", code, str(tmp_path), parent], timeout=15).returncode == 73
    assert not graph.exists(parent) and not graph.exists(child)
    with pytest.raises(ValueError, match="fact_revoked"):
        _add(graph)
    assert not (graph.dir / ".revoke.json").exists()
    assert (graph.revoked_dir / f"{child}.md").is_file()
