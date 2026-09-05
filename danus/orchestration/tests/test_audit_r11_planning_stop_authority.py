from __future__ import annotations

from danus.orchestration import planning


def test_planning_step_blocks_all_mutation_on_budget_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(planning.L, "project_dir", lambda project: tmp_path)
    seen = {}

    def control(project, *, apply, token_budget, trigger):
        seen.update(
            project=project,
            apply=apply,
            token_budget=token_budget,
            trigger=trigger,
        )
        return {"project_state": "RUNNING", "budget_stop": "resource_token_budget"}

    mutated = []
    monkeypatch.setattr(planning.cli, "_do_supervise_unlocked", control)
    monkeypatch.setattr(
        planning,
        "_planning_step_unlocked",
        lambda project: mutated.append(project) or [{"action": "SHOULD_NOT_RUN"}],
    )

    assert planning.planning_step("P", token_budget=123) == []
    assert mutated == []
    assert seen == {
        "project": "P",
        "apply": False,
        "token_budget": 123,
        "trigger": "planning_stop_check",
    }


def test_planning_step_does_not_treat_complete_as_budget_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(planning.L, "project_dir", lambda project: tmp_path)
    monkeypatch.setattr(
        planning.cli,
        "_do_supervise_unlocked",
        lambda project, **kwargs: {"project_state": "COMPLETE", "budget_stop": None},
    )
    expected = [{"action": "PLAN_PROPOSAL", "result": "captured"}]
    monkeypatch.setattr(planning, "_planning_step_unlocked", lambda project: expected)

    assert planning.planning_step("P", token_budget=5) == expected


def test_watch_research_forwards_budget_to_initial_and_followup_planning(monkeypatch):
    budgets = []

    def step(project, *, token_budget=0):
        budgets.append(token_budget)
        return []

    monkeypatch.setattr(planning, "planning_step", step)
    monkeypatch.setattr(
        planning.cli,
        "watch_supervisor",
        lambda project, **kwargs: iter(
            [{"project_state": "COMPLETE", "decisions": [], "budget_stop": None}]
        ),
    )

    reports = list(planning.watch_research("P", heartbeat=1, poll=0.01, token_budget=77))
    assert len(reports) == 1
    assert budgets == [77, 77]


def test_research_once_forwards_budget_to_planning(monkeypatch, capsys):
    budgets = []
    monkeypatch.setattr(
        planning,
        "initialize_research",
        lambda project, **kwargs: {"project": project},
    )
    monkeypatch.setattr(
        planning,
        "planning_step",
        lambda project, *, token_budget=0: budgets.append(token_budget) or [],
    )
    monkeypatch.setattr(
        planning.cli,
        "do_supervise",
        lambda project, **kwargs: {"project": project, "budget_stop": "resource_token_budget"},
    )

    assert planning.main(["P", "--once", "--token-budget", "91"]) == 0
    assert budgets == [91]
    capsys.readouterr()
