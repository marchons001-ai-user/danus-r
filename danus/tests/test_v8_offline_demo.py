"""Offline public demo must exercise real Host gates without any provider calls."""
from scripts.demo_v8_offline import run_demo


def test_offline_synthetic_demo_rejects_premature_finish_and_passes_checks():
    result = run_demo()
    assert result["synthetic_only"] is True
    assert result["autonomous_agent_run"] is False
    assert result["api_calls"] == 0
    assert result["proposal_status"] == "APPROVE"
    assert result["validation"] == "PASSED"
    assert set(result["checks"]) == {"assets", "row_count", "mean_recompute"}
    assert all(v == "PASSED" for v in result["checks"].values())
    assert result["pre_review_finish_rejected"] is True
    assert result["final_verdict"] == "INCONCLUSIVE"
    assert result["done"] is True
    assert result["events"][-1] == "FINISH"
    assert result["roles"] == [
        "researcher", "reviewer", "experimenter", "researcher", "reviewer",
    ]
