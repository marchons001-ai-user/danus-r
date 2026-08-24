"""Offline tests for the unified analytical/empirical fact submission gate.

No real verifier or LLM is launched. The tests mock semantic verdicts and use a
local stdlib HTTP server only to confirm the empirical route/payload wiring.
"""

from __future__ import annotations

import http.server
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from danus.core import EvidenceStore, FactGraph, GlobalMemory
from danus.gateway import server


@contextmanager
def _env(**kv):
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _mock_empirical(result=None, raise_exc=None):
    orig = server._verify_empirical

    def fake(**kwargs):
        if raise_exc is not None:
            raise raise_exc
        return result or {
            "verification_type": "empirical",
            "verification_report": {
                "summary": "mock empirical accept",
                "critical_errors": [],
                "gaps": [],
                "deterministic_checks": [{"name": "mock", "passed": True}],
            },
            "verdict": "correct",
            "repair_hints": "",
        }

    server._verify_empirical = fake
    try:
        yield
    finally:
        server._verify_empirical = orig


@contextmanager
def _mock_analytical(result=None):
    orig = server._verify
    server._verify = lambda statement, proof: result or {
        "verification_report": {"summary": "mock analytical accept"},
        "verdict": "correct",
        "repair_hints": "",
    }
    try:
        yield
    finally:
        server._verify = orig


def _evidence(project: Path) -> str:
    store = EvidenceStore(project)
    protocol_id = store.freeze_protocol(
        question="Does metric improve?",
        hypothesis="B exceeds A",
        matrix={"conditions": ["A", "B"]},
        primary_metrics=["rmse"],
        decision_rule="accept directional claim only if B < A",
        author="reviewer",
    )
    return store.add_record(
        protocol_id=protocol_id,
        author="experimenter",
        code_commit="abc123",
        data_ref="synthetic-v1",
        config_ref="cfg-v1",
        matrix_coverage={"planned": 2, "completed": 2, "failed": 0},
        primary_metrics={"A_rmse": 0.18, "B_rmse": 0.12},
    )


def _source(gm: GlobalMemory, claim: str) -> str:
    return gm.append(
        "conclusion", claim=claim, evidence="Evidence packet ev supports this candidate.",
        author="researcher",
    )


def _entry(gm: GlobalMemory, kind: str, entry_id: str):
    return next(e for e in gm.read(kind) if e["id"] == entry_id)


def test_empirical_accept_writes_unified_fact_and_promotes_source():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AUTHOR="researcher", DANUS_PROBLEM_ID="P",
        DANUS_VERIFY_URL="http://mock/verify",
    ), _mock_empirical():
        project = Path(d)
        evidence_id = _evidence(project)
        gm = GlobalMemory(project)
        source_id = _source(gm, "B has lower RMSE than A on synthetic-v1.")

        out = server.fact_submit(
            statement="B has lower RMSE than A on synthetic-v1.",
            fact_type="empirical",
            scope={"dataset": "synthetic-v1", "conditions": ["A", "B"]},
            evidence_refs=[evidence_id],
            evidence_summary="A_rmse=0.18; B_rmse=0.12.",
            source_id=source_id,
        )
        assert out["accepted"] is True and out["fact_id"]

        fg = FactGraph(project)
        fid = out["fact_id"]
        assert fg.fact_type(fid) == "empirical"
        assert fg.scope(fid)["dataset"] == "synthetic-v1"
        assert fg.evidence_refs(fid) == [evidence_id]

        promoted = _entry(gm, "conclusion", source_id)
        assert promoted["status"] == "verified" and promoted["fact_id"] == fid

        trace = gm.read("verification")[-1]
        assert trace["verification_type"] == "empirical"
        assert trace["fact_id"] == fid and trace["verdict"] == "correct"
        assert trace["links"]["evidence_refs"] == [evidence_id]
        assert trace["verification_report"]["deterministic_checks"][0]["passed"] is True


def test_empirical_reject_writes_nothing_and_does_not_promote_source():
    wrong = {
        "verification_type": "empirical",
        "verification_report": {"summary": "claim exceeds scope", "critical_errors": [], "gaps": [{"location": "scope", "issue": "too broad"}]},
        "verdict": "wrong",
        "repair_hints": "narrow the claim",
    }
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AUTHOR="researcher", DANUS_PROBLEM_ID="P",
        DANUS_VERIFY_URL="http://mock/verify",
    ), _mock_empirical(wrong):
        project = Path(d)
        evidence_id = _evidence(project)
        gm = GlobalMemory(project)
        source_id = _source(gm, "B is universally superior.")
        out = server.fact_submit(
            statement="B is universally superior.", fact_type="empirical",
            scope={"dataset": "synthetic-v1"}, evidence_refs=[evidence_id],
            source_id=source_id,
        )
        assert out["accepted"] is False and out["verdict"] == "wrong"
        assert FactGraph(project).list() == []
        assert _entry(gm, "conclusion", source_id)["status"] == "unverified"
        assert gm.read("verification")[-1]["verification_type"] == "empirical"


def test_empirical_accept_but_graph_write_failure_is_traced_not_promoted():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AUTHOR="researcher", DANUS_PROBLEM_ID="P",
        DANUS_VERIFY_URL="http://mock/verify",
    ), _mock_empirical():
        project = Path(d)
        evidence_id = _evidence(project)
        fg = FactGraph(project)
        base = fg.add(problem_id="P", author="w", statement="A", proof="proof A")
        fg.revoke(base, reason="bad predecessor")
        gm = GlobalMemory(project)
        source_id = _source(gm, "candidate empirical dependent")

        out = server.fact_submit(
            statement="candidate empirical dependent", fact_type="empirical",
            scope={"dataset": "synthetic-v1"}, evidence_refs=[evidence_id],
            predecessors=[base], source_id=source_id,
        )
        assert out["accepted"] is True and out["fact_id"] is None and out["write_error"]
        assert _entry(gm, "conclusion", source_id)["status"] == "unverified"
        trace = gm.read("verification")[-1]
        assert trace["verdict"] == "correct" and trace["fact_id"] is None
        assert trace["write_error"]


def test_analytical_path_still_promotes_source_after_real_write():
    with tempfile.TemporaryDirectory() as d, _env(
        DANUS_PROJECT_DIR=d, DANUS_AUTHOR="worker_high", DANUS_PROBLEM_ID="P",
        DANUS_VERIFY_URL="http://mock/verify",
    ), _mock_analytical():
        project = Path(d)
        gm = GlobalMemory(project)
        source_id = gm.append("conclusion", claim="A holds", evidence="proof A", author="worker_high")
        out = server.fact_submit(statement="A holds", proof="proof A", source_id=source_id)
        assert out["accepted"] is True and out["fact_id"]
        assert FactGraph(project).fact_type(out["fact_id"]) == "analytical"
        assert _entry(gm, "conclusion", source_id)["status"] == "verified"
        assert gm.read("verification")[-1]["verification_type"] == "analytical"


def test_bad_inputs_stop_before_any_verifier_call():
    original_math = server._verify
    original_emp = server._verify_empirical
    calls = {"math": 0, "emp": 0}

    def math(*args, **kwargs):
        calls["math"] += 1
        raise AssertionError("math verifier must not run")

    def emp(*args, **kwargs):
        calls["emp"] += 1
        raise AssertionError("empirical verifier must not run")

    server._verify = math
    server._verify_empirical = emp
    try:
        with tempfile.TemporaryDirectory() as d, _env(DANUS_PROJECT_DIR=d):
            assert server.fact_submit(statement="x", fact_type="unknown")["verdict"] == "error"
            assert server.fact_submit(statement="x", fact_type="analytical", proof="")["verdict"] == "error"
            assert server.fact_submit(statement="x", fact_type="empirical", scope={}, evidence_refs=["e"])["verdict"] == "error"
            assert server.fact_submit(statement="x", fact_type="empirical", scope={"d": 1}, evidence_refs=[])["verdict"] == "error"
        assert calls == {"math": 0, "emp": 0}
    finally:
        server._verify = original_math
        server._verify_empirical = original_emp


def test_empirical_http_route_and_payload_are_wired_without_llm():
    captured = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            captured["path"] = self.path
            captured["body"] = json.loads(self.rfile.read(n).decode("utf-8"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"verdict": "wrong", "repair_hints": "mock", "verification_report": {}}).encode("utf-8"))

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as d, _env(
            DANUS_PROJECT_DIR=d,
            DANUS_VERIFY_URL=f"http://127.0.0.1:{srv.server_address[1]}/verify",
            DANUS_VERIFY_TIMEOUT="5",
        ):
            out = server._verify_empirical(
                statement="claim", scope={"dataset": "D"},
                evidence_refs=["ev123"], evidence_summary="summary",
            )
            assert out["verdict"] == "wrong"
            assert captured["path"] == "/verify/empirical"
            assert captured["body"]["statement"] == "claim"
            assert captured["body"]["scope"] == {"dataset": "D"}
            assert captured["body"]["evidence_refs"] == ["ev123"]
            assert captured["body"]["project_dir"] == str(Path(d).resolve())
    finally:
        srv.shutdown()


def main() -> None:
    tests = [
        test_empirical_accept_writes_unified_fact_and_promotes_source,
        test_empirical_reject_writes_nothing_and_does_not_promote_source,
        test_empirical_accept_but_graph_write_failure_is_traced_not_promoted,
        test_analytical_path_still_promotes_source_after_real_write,
        test_bad_inputs_stop_before_any_verifier_call,
        test_empirical_http_route_and_payload_are_wired_without_llm,
    ]
    for fn in tests:
        fn()
        print(f"  [ok] {fn.__name__}")
    print("ALL SCIENTIFIC GATE TESTS PASSED")


if __name__ == "__main__":
    main()
