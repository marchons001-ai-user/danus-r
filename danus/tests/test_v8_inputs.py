"""V8 input provenance: small offline regression tests, no Codex calls."""
import hashlib
import json

import pytest

from danus import v8, v8_inputs


def create(tmp_path):
    source = tmp_path / "original" / "data" / "cx"
    source.mkdir(parents=True)
    (source / "neurons.csv").write_text("bodyId,type,instance\n1,EPG,EPG_R\n")
    (source / "connections.csv").write_text("bodyId_pre,bodyId_post,weight\n1,2,4\n")
    script = tmp_path / "original" / "extract_cx.py"
    script.write_text("print('example extractor')\n")
    store = v8.Store(tmp_path / "runtime" / "v8" / "study")
    v8.initialize(store, "Research connections using externally supplied CSV inputs")
    return store, source, script


def stage(store, source, script):
    return v8_inputs.stage_inputs(
        store, source, ["neurons.csv", "connections.csv"], "hemibrain:v1.1",
        extraction_script=script)


def assign(store, role, purpose="work", **kwargs):
    return v8.apply_decision(store, {
        "action": "dispatch", "role": role, "purpose": purpose,
        "task": "Analyze documented staged data",
        "reason": "Required to assess scientific evidence", **kwargs,
    }, "low")


def result(store, task, **fields):
    store.append("RUNNING", task_id=task)
    v8._worker_result(store, task, {"action": "complete", "summary": "Checked staged data",
                                    **fields})


def test_staged_inputs_have_machine_verified_source_and_worker_handoff(tmp_path, monkeypatch):
    store, source, script = create(tmp_path)
    record = stage(store, source, script)
    state = store.state()
    assert len(state["inputs"]["files"]) == 2
    assert store.read()[-1]["kind"] == "INPUT_STAGE"
    manifest = json.loads(
        (store.root / "workspace/input/PROVENANCE.json").read_text())
    assert manifest["schema"] == "danus-v8-input-provenance-v1"
    assert manifest["dataset_label"] == "hemibrain:v1.1"
    assert "not_verified" in manifest
    assert manifest["source"]["extraction_references"]["extraction_script"]["sha256"] == (
        hashlib.sha256(script.read_bytes()).hexdigest())
    assert [entry["sha256"] for entry in state["inputs"]["files"]] == [
        hashlib.sha256((source / n).read_bytes()).hexdigest()
        for n in ("neurons.csv", "connections.csv")]
    assert record["manifest_sha256"] == hashlib.sha256(
        (store.root / "workspace/input/PROVENANCE.json").read_bytes()).hexdigest()
    v8_inputs.verify_inputs(store)

    task = assign(store, "researcher")
    assert v8._brief_state(store)["inputs"]["dataset_label"] == "hemibrain:v1.1"
    prompts = []
    def fake_call(model, effort, prompt, workdir, log_path, *, writable=False, timeout=1200):
        prompts.append(prompt)
        assert not writable
        return {"action": "complete", "summary": "Manifest checked with bounded origin claims"}
    monkeypatch.setattr(v8, "_call", fake_call)
    v8._worker(store, task, timeout=40)
    assert store.state()["tasks"][task]["status"] == "COMPLETE"
    assert "input/PROVENANCE.json" in prompts[0]
    assert "hemibrain:v1.1" in prompts[0]


def test_stage_is_exactly_once_and_before_tasks(tmp_path):
    store, source, script = create(tmp_path)
    stage(store, source, script)
    with pytest.raises(PermissionError, match="already frozen"):
        stage(store, source, script)
    assign(store, "researcher")
    with pytest.raises(PermissionError, match="before first task"):
        v8_inputs.stage_inputs(store, source, ["neurons.csv"], "new dataset")


def test_preexisting_input_folder_not_silently_overwritten(tmp_path):
    store, source, script = create(tmp_path)
    path = store.root / "workspace/input"
    path.mkdir()
    (path / "neurons.csv").write_text("operator files without source record")
    with pytest.raises(FileExistsError, match="never overwrite"):
        stage(store, source, script)
    assert store.state()["inputs"] is None


def test_input_mutation_prevents_next_supervisor_decision(tmp_path, monkeypatch):
    store, source, script = create(tmp_path)
    stage(store, source, script)
    (store.root / "workspace/input/neurons.csv").write_text("tampered")
    def must_not_run(*args, **kwargs):
        pytest.fail("No model call allowed after input integrity failure")
    monkeypatch.setattr(v8, "_supervisor", must_not_run)
    assert v8.step(store) == "HALT"
    assert "INPUT_INTEGRITY" in store.state()["halt"]
    assert not store.state()["done"]


def test_manifest_tampering_is_detected(tmp_path):
    store, source, script = create(tmp_path)
    stage(store, source, script)
    p = store.root / "workspace/input/PROVENANCE.json"
    payload = json.loads(p.read_text())
    payload["verified_scope"] = "falsely authenticated upstream"
    p.write_text(json.dumps(payload))
    with pytest.raises(PermissionError, match="manifest"):
        v8_inputs.verify_inputs(store)


def test_experimenter_cannot_claim_input_as_output(tmp_path):
    store, source, script = create(tmp_path)
    stage(store, source, script)
    researcher = assign(store, "researcher")
    result(store, researcher, proposal="Analyze documented connectivity",
           required_checks=[{"id": "files", "kind": "asset_integrity"}])
    proposal_id = next(iter(store.state()["proposals"]))
    reviewer = assign(store, "reviewer", "review_experiment", proposal_id=proposal_id)
    result(store, reviewer, verdict="APPROVE")
    exp = assign(store, "experimenter", "experiment", proposal_id=proposal_id)
    store.append("RUNNING", task_id=exp)
    with pytest.raises(PermissionError, match="staged input files"):
        v8._worker_result(store, exp, {
            "action": "complete", "summary": "Invalid output registration",
            "artifacts": ["input/neurons.csv"]})
    assert not store.state()["assets"]
    alias = store.root / "workspace" / "input_alias.csv"
    alias.symlink_to(store.root / "workspace" / "input/neurons.csv")
    with pytest.raises(PermissionError, match="staged input files"):
        v8._worker_result(store, exp, {
            "action": "complete", "summary": "Alias must not bypass origin freeze",
            "artifacts": ["input_alias.csv"]})
