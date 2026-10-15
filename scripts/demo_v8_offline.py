"""Deterministic offline V8 gate demo. SYNTHETIC DATA; NOT AN AUTONOMOUS RUN.

Uses the same Store, decision gates and Host checks as production. It
deliberately scripts role responses; no LLM, model training, Codex, or network.
The entire temporary workspace is removed automatically.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from danus import v8


def run_demo():
    with TemporaryDirectory(prefix="danus-r-offline-demo-") as directory:
        store = v8.Store(Path(directory) / "demo")
        v8.initialize(store, "Verify a synthetic two-row CSV. This is NOT scientific evidence.")

        def dispatch(role, instruction, purpose="work", **fields):
            return v8.apply_decision(store, {
                "action": "dispatch", "role": role, "task": instruction,
                "purpose": purpose, "reason": "offline scripted gate demonstration",
                **fields,
            }, "low")

        def complete(task_id, **fields):
            store.append("RUNNING", task_id=task_id)
            v8._worker_result(store, task_id, {
                "action": "complete", "summary": "Scripted synthetic demonstration",
                **fields,
            })

        checks = [
            {"id": "assets", "kind": "asset_integrity"},
            {"id": "row_count", "kind": "csv_rows", "path": "synthetic/metrics.csv",
             "expected_rows": 2},
            {"id": "mean_recompute", "kind": "csv_mean",
             "path": "synthetic/metrics.csv", "column": "mse",
             "report_path": "synthetic/report.json", "report_key": "mse",
             "abs_tolerance": 1e-12},
        ]
        research = dispatch("researcher", "Propose a two-row deterministic protocol")
        complete(research, proposal="Synthetic mean verification; no scientific claims.",
                 required_checks=checks)
        proposal_id = next(iter(store.state()["proposals"]))
        review = dispatch("reviewer", "Approve exact typed checks",
                          "review_experiment", proposal_id=proposal_id)
        complete(review, verdict="APPROVE")

        experiment = dispatch("experimenter", "Register synthetic outputs",
                              "experiment", proposal_id=proposal_id)
        workspace = store.root / "workspace" / "synthetic"
        workspace.mkdir(parents=True)
        (workspace / "metrics.csv").write_text("mse\n1\n3\n", encoding="utf-8")
        (workspace / "report.json").write_text('{"mse": 2.0}\n', encoding="utf-8")
        complete(experiment, artifacts=[
            "synthetic/metrics.csv", "synthetic/report.json",
        ])
        validation = store.state()["latest_validation"]
        assert validation["status"] == "PASSED", validation

        # Independent final review must precede FINISH even if Host PASSed.
        finish_reason = "Cannot omit independent Reviewer"
        try:
            v8.apply_decision(store, {"action": "finish",
                              "reason": finish_reason}, "low")
        except PermissionError as error:
            guard = str(error)
            assert "final review" in guard
        else:
            raise AssertionError("FINISH bypassed independent review")

        interpretation = dispatch("researcher", "Interpret synthetic mean",
                                  "interpret")
        complete(interpretation)
        final = dispatch("reviewer", "Review evidence limits", "final_review")
        complete(final, verdict="INCONCLUSIVE")
        v8.apply_decision(store, {"action": "finish",
                          "reason": "Independent review complete; synthetic only"}, "low")

        state = store.state()
        assert state["done"] and not state["halt"]
        assert state["final_review"]["verdict"] == "INCONCLUSIVE"
        assert all(t["status"] == "COMPLETE" for t in state["tasks"].values())
        return {
            "synthetic_only": True,
            "autonomous_agent_run": False,
            "api_calls": 0,
            "events": [event["kind"] for event in store.read()],
            "roles": [task["role"] for task in state["tasks"].values()],
            "proposal_status": state["proposals"][proposal_id]["status"],
            "validation": validation["status"],
            "checks": {entry["id"]: entry["status"] for entry in validation["checks"]},
            "pre_review_finish_rejected": True,
            "final_verdict": state["final_review"]["verdict"],
            "done": state["done"],
        }


if __name__ == "__main__":
    print(json.dumps(run_demo(), indent=2, sort_keys=True))
