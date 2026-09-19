"""Offline regressions for authority, lifecycle, serialization, and recovery."""
from __future__ import annotations

import json
import multiprocessing as mp
import os
from pathlib import Path
import threading

import pytest

from danus.core.handoff import HandoffRequests

from danus.core import ProgressLedger, ResearchPlanGraph
from danus.orchestration import cli, autonomy
from danus.orchestration.control import reconcile


def commit(graph, ops):
    cid = graph.propose_change(base_plan_version=graph.snapshot()['plan_version'], proposer='researcher', reason='bounded revision', operations=ops)
    graph.review_change(cid, reviewer='reviewer', verdict='APPROVE', rationale='approved scope')
    graph.commit_change(cid, committer='supervisor')
    return cid


def project(tmp_path, monkeypatch):
    monkeypatch.setenv('DANUS_AGENTS_ROOT', str(tmp_path))
    p = tmp_path / 'P'
    p.mkdir()
    return p, ResearchPlanGraph.create(p, node_id='root', title='Root'), ProgressLedger(p)


def worker(p, name='r', role='researcher'):
    w = p / 'workers' / name
    w.mkdir(parents=True, exist_ok=True)
    (w / '.role').write_text(f'ROLE={role}\nREASONING_EFFORT=low\n', encoding='utf-8')
    return w


def task(p, version, node, text='bounded work\n', role='researcher'):
    f = p / 'tasks' / f'v{version}' / role / f'{node}.md'
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding='utf-8')
    return text


def done(ledger, who='r'):
    a = ledger.current(who)
    return ledger.update(worker=who, state='DONE', actor='runtime', outcome='COMPLETE', expected_assignment_id=a['assignment_id'])


def test_A01_manual_reopen_must_not_reuse_completed_assignment(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, '_start_one', lambda wl: 'started')
    p, graph, ledger = project(tmp_path, monkeypatch)
    worker(p)
    cli.do_assign('P/r', task(p, 1, 'root'), node_id='root')
    done(ledger)
    assert cli.do_supervise('P', apply=True)['project_state'] == 'COMPLETE'
    assert cli.do_supervise('P', reopen_node='root')['project_state'] == 'RUNNING'
    report = cli.do_supervise('P', apply=True)
    assert ledger.node_states()['root']['state'] == 'OPEN', report['autonomy_applied']


def test_A01_fresh_assignment_can_complete_after_reopen(tmp_path, monkeypatch):
    p, graph, ledger = project(tmp_path, monkeypatch)
    worker(p)
    text = task(p, 1, 'root')
    old = cli.do_assign('P/r', text, node_id='root')
    done(ledger)
    cli.do_supervise('P', apply=True)
    ledger.set_node_state('root', state='OPEN', actor='human')
    new = cli.do_assign('P/r', text, node_id='root')
    assert new['assignment_id'] != old['assignment_id']
    done(ledger)
    assert cli.do_supervise('P', apply=True)['project_state'] == 'COMPLETE'


def test_A01_reassignment_before_closure_write_invalidates_old_authority(tmp_path, monkeypatch):
    p, graph, ledger = project(tmp_path, monkeypatch)
    worker(p)
    old = cli.do_assign('P/r', task(p, 1, 'root'), node_id='root')
    done(ledger)
    original = ProgressLedger.set_node_state
    def reassign_before_write(self, *args, **kwargs):
        cli.do_assign('P/r', 'new bounded assignment', node_id='root')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(ProgressLedger, 'set_node_state', reassign_before_write)
    result = autonomy._auto_complete(p, {'action': 'REVIEW', 'reason': 'completion_not_recorded',
        'worker': 'r', 'node_id': 'root', 'assignment_id': old['assignment_id']})
    assert result['result'] == 'assignment_changed'
    assert ledger.completed_nodes() == []


def test_A02_recompleting_upstream_does_not_restore_old_dependents(tmp_path, monkeypatch):
    p, graph, ledger = project(tmp_path, monkeypatch)
    commit(graph, [{'op': 'add_node', 'node_id': n, 'title': n, 'parent': 'root', 'depends_on': deps}
                   for n, deps in [('a', []), ('b', ['a']), ('c', ['b'])]])
    for node in ['a', 'b']:
        ledger.set_node_state(node, state='DONE', actor='human')
    ledger.set_node_state('a', state='OPEN', actor='human')
    ledger.set_node_state('a', state='DONE', actor='human')
    assert ledger.node_states()['b']['state'] == 'OPEN'
    report = reconcile(p, [])
    assert 'c' not in report['frontier']
    assert 'b' in report['frontier']


def test_A04_contract_and_task_do_not_survive_cancel_reactivation(tmp_path, monkeypatch):
    p, graph, ledger = project(tmp_path, monkeypatch)
    task(p, 1, 'root')
    contract = ledger.submit_implementation_contract(proposer='r', node_id='root', plan_version=1,
        goal='bounded implementation', must_preserve_invariants=['result'],
        forbidden_behaviors=['extra execution'], acceptance_conditions=['one result'])
    ledger.review_implementation_contract(contract['contract_id'], reviewer='v', verdict='APPROVE', rationale='bounded')
    commit(graph, [{'op': 'set_state', 'node_id': 'root', 'state': 'CANCELLED'}])
    commit(graph, [{'op': 'set_state', 'node_id': 'root', 'state': 'ACTIVE'}])
    assert ledger.implementation_contract(contract['contract_id'])['status'] == 'STALE'
    assert autonomy._task_spec(p, 'root')['status'] == 'missing'


@pytest.mark.parametrize('subject', ['PLAN_CHANGE', 'IMPLEMENTATION_CONTRACT'])
def test_A06_old_review_attempt_cannot_authorize_new_attempt(tmp_path, monkeypatch, subject):
    from danus.gateway import server

    p, graph, ledger = project(tmp_path, monkeypatch)
    if subject == 'PLAN_CHANGE':
        ident = graph.propose_change(base_plan_version=1, proposer='r', reason='bounded',
            operations=[{'op': 'add_node', 'node_id': 'a', 'title': 'A', 'parent': 'root'}])
        review_call = lambda: server.plan_review_change(ident, verdict='APPROVE', rationale='bounded')
    else:
        contract = ledger.submit_implementation_contract(proposer='r', node_id='root', plan_version=1,
            goal='bounded implementation', must_preserve_invariants=['result'],
            forbidden_behaviors=['extra execution'], acceptance_conditions=['one result'])
        ident = contract['contract_id']
        review_call = lambda: server.implementation_review_contract(ident, 'APPROVE', 'bounded')
    handoffs = HandoffRequests(ledger)
    review = handoffs.open(kind='REVIEW', plan_version=1, node_id='root', subject_type=subject,
        subject_id=ident, requested_by='supervisor', source_role='researcher', request='review exact subject', reason='review required')
    old = ledger.assign(worker='v', role='reviewer', node_id='root', plan_version=1,
        task=review['request'], handoff_id=review['handoff_id'])
    monkeypatch.setenv('DANUS_PROJECT_DIR', str(p))
    monkeypatch.setenv('DANUS_AUTHOR', 'v')
    monkeypatch.setenv('DANUS_ROLE', 'reviewer')
    monkeypatch.setenv('DANUS_ASSIGNMENT_ID', old['assignment_id'])
    review_call()
    handoffs.reopen(review['handoff_id'], assignment_id=old['assignment_id'], actor='human', reason='retry')
    new = ledger.assign(worker='v', role='reviewer', node_id='root', plan_version=1,
        task=review['request'], handoff_id=review['handoff_id'])
    with pytest.raises(ValueError, match='current Reviewer assignment'):
        review_call()
    monkeypatch.setenv('DANUS_ASSIGNMENT_ID', new['assignment_id'])
    with pytest.raises(ValueError, match='conflicting replay'):
        review_call()
    done(ledger, 'v')
    report = reconcile(p, [])
    assert not any(action['kind'] in {'ASSIGN_CONTRACT', 'COMMIT_PLAN_CHANGE', 'RESOLVE_HANDOFF'}
                   for action in report['actions'])


@pytest.mark.parametrize('applied', [False, True])
def test_A07_failed_or_applied_checkpoint_does_not_suppress_new_work(tmp_path, monkeypatch, applied):
    from danus.execution import context as C

    p, graph, ledger = project(tmp_path, monkeypatch)
    w = worker(p)
    cli.do_assign('P/r', 'bounded work', node_id='root')
    ledger.update(worker='r', state='RUNNING', actor='runtime')
    outcome = 'NEEDS_REVIEW' if applied else 'COMPLETE'
    C.write_checkpoint(w, role='researcher', round_no=1,
        telemetry={'last_agent_message': f'prior result\nDANUS_OUTCOME: {outcome}'},
        returncode=0 if applied else 1, terminal_outcome=outcome)
    checkpoint = C.checkpoint_for_prompt(w, 'researcher')
    if applied:
        ledger.update(worker='r', state='BLOCKED', actor='runtime', outcome=outcome,
            checkpoint_id=checkpoint['checkpoint_id'])
        ledger.update(worker='r', state='RUNNING', actor='human', note='resume after review')
    counter = fake_backend(tmp_path, monkeypatch)
    process = mp.get_context('fork').Process(target=_worker_run, args=(w, False))
    process.start()
    process.join(10)
    if process.is_alive():
        process.kill()
        process.join()
    assert process.exitcode == 0
    assert counter.read_text().splitlines() == ['executed']
    assert ledger.current('r')['state'] == 'DONE'


def test_A08_revision_file_without_current_commit_is_not_completion_proof(tmp_path, monkeypatch):
    import copy

    p, graph, ledger = project(tmp_path, monkeypatch)
    ident = graph.propose_change(base_plan_version=1, proposer='r', reason='bounded',
        operations=[{'op': 'add_node', 'node_id': 'a', 'title': 'A', 'parent': 'root'}])
    graph.review_change(ident, reviewer='v', verdict='APPROVE', rationale='bounded')
    record = graph.get_change(ident)
    candidate = copy.deepcopy(graph.snapshot())
    graph._apply_operations(candidate, record['operations'])
    candidate.update(plan_version=2, committed_change_id=ident)
    graph._write_revision(candidate)
    assert graph.committed_version(record) is None
    assert graph.pending_changes()[0]['status'] == 'APPROVED'
    assert graph.snapshot()['plan_version'] == 1
    assert graph.commit_change(ident, committer='supervisor') == 2


def test_A09_stop_during_luna_defers_materialization_and_replay_respects_pause(tmp_path, monkeypatch):
    import time
    from danus.orchestration import supervisor_llm as S
    from danus.orchestration.tests.test_s6_scientific_route_closure import _ambiguity, _luna

    monkeypatch.setenv('DANUS_AGENTS_ROOT', str(tmp_path))
    p = tmp_path / 'P'
    p.mkdir()
    ledger, source, action = _ambiguity(p)
    calls = []
    def luna(prompt):
        calls.append(prompt)
        (p / '.run_deadline').write_text(str(time.time() - 1))
        return _luna('RETHINK')
    monkeypatch.setattr(S, '_run_luna', luna)
    first = S.resolve_scientific_routes(p, [action])
    assert first[0]['status'] == 'persisted_unmaterialized'
    assert not any(item['kind'] == 'RETHINK' for item in HandoffRequests(ledger).all())
    (p / '.run_deadline').unlink()
    cli.do_pause('P')
    assert S.resolve_scientific_routes(p, [action])[0]['status'] == 'blocked'
    (p / '.pause').unlink()
    final = S.resolve_scientific_routes(p, [action])
    assert final[0]['replayed']
    assert len(calls) == 1


def test_A02_reopened_dependency_must_block_transitive_successor(tmp_path, monkeypatch):
    p, graph, ledger = project(tmp_path, monkeypatch)
    commit(graph, [{'op':'add_node','node_id':n,'title':n,'parent':'root','depends_on':deps} for n,deps in [('a',[]),('b',['a']),('c',['b'])]])
    ledger.set_node_state('a', state='DONE', actor='supervisor')
    ledger.set_node_state('b', state='DONE', actor='supervisor')
    ledger.set_node_state('a', state='OPEN', actor='supervisor')
    worker(p)
    task(p, 2, 'c')
    launches=[]
    monkeypatch.setattr(cli, '_start_one', lambda wl: launches.append(wl.name) or 'started')
    report=cli.do_supervise('P', apply=True)
    assert not ledger.assignments(), {'frontier':report['frontier'],'assignments':ledger.assignments(),'launches':launches}


def test_A03_stale_done_must_not_permanently_block_new_assignment(tmp_path, monkeypatch):
    p, graph, ledger = project(tmp_path, monkeypatch)
    commit(graph,[{'op':'add_node','node_id':n,'title':n,'parent':'root'} for n in ['a','b']])
    worker(p)
    cli.do_assign('P/r', task(p,2,'a'),node_id='a')
    old=done(ledger)
    commit(graph,[{'op':'set_dependencies','node_id':'a','depends_on':['b']}])
    ledger.set_node_state('b',state='DONE',actor='supervisor')
    task(p,3,'a')
    monkeypatch.setattr(cli,'_start_one',lambda wl:'started')
    reports=[cli.do_supervise('P',apply=True) for _ in range(3)]
    assert ledger.current('r')['assignment_id'] != old['assignment_id'], [r['autonomy_applied'] for r in reports]


def test_A04_cancel_reactivate_must_not_revive_old_assignment(tmp_path, monkeypatch):
    p, graph, ledger = project(tmp_path, monkeypatch)
    worker(p)
    cli.do_assign('P/r', task(p,1,'root'),node_id='root')
    old=ledger.current('r')
    commit(graph,[{'op':'set_state','node_id':'root','state':'CANCELLED'}])
    commit(graph,[{'op':'set_state','node_id':'root','state':'ACTIVE'}])
    with pytest.raises(ValueError, match='STALE'):
        ledger.update(worker='r',state='DONE',outcome='COMPLETE',actor='runtime',expected_assignment_id=old['assignment_id'])
    result=ledger.current('r')
    report=cli.do_supervise('P',apply=True)
    assert report['project_state'] != 'COMPLETE', {'old_result':result,'report':report['autonomy_applied']}


def test_A05_concurrent_review_must_not_overwrite_committed_approval(tmp_path, monkeypatch):
    p, graph, ledger = project(tmp_path, monkeypatch)
    cid=graph.propose_change(base_plan_version=1,proposer='r',reason='new route',operations=[{'op':'add_node','node_id':'a','title':'A','parent':'root'}])
    ready=threading.Event()
    release=threading.Event()
    rejected=threading.Event()
    slow=ResearchPlanGraph(p)
    original=slow.snapshot
    def paused_snapshot():
        snapshot=original()
        ready.set()
        assert release.wait(5)
        return snapshot
    slow.snapshot=paused_snapshot
    errors=[]
    def approve():
        slow.review_change(cid,reviewer='reviewer',verdict='APPROVE',rationale='approved')
    def reject():
        try:
            graph.review_change(cid,reviewer='reviewer',verdict='REJECT',rationale='competing verdict')
        except ValueError as exc:
            errors.append(str(exc))
        finally:
            rejected.set()
    first=threading.Thread(target=approve)
    second=threading.Thread(target=reject)
    first.start()
    assert ready.wait(5)
    second.start()
    assert not rejected.wait(0.05)
    release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert errors
    graph.commit_change(cid,committer='supervisor')
    assert graph.get_change(cid)['status']=='COMMITTED'


def test_A06_generic_reviewer_must_not_authorize_unassigned_contract(tmp_path, monkeypatch):
    from danus.core.handoff import HandoffRequests
    from danus.gateway import server as gateway
    p, graph, ledger = project(tmp_path, monkeypatch)
    commit(graph,[{'op':'add_node','node_id':n,'title':n,'parent':'root'} for n in ['a','b']])
    source=ledger.assign(worker='r',role='researcher',node_id='a',plan_version=2,task='source work')
    ledger.update(worker='r',state='BLOCKED',actor='runtime',outcome='NEEDS_REVIEW')
    handoff=HandoffRequests(ledger).open(kind='REVIEW',plan_version=2,node_id='a',subject_type='ASSIGNMENT',subject_id=source['assignment_id'],source_assignment_id=source['assignment_id'],requested_by='r',source_role='researcher',request='classify source ambiguity only',reason='generic review')
    ledger.assign(worker='v',role='reviewer',node_id='a',plan_version=2,task=handoff['request'],handoff_id=handoff['handoff_id'])
    contract=ledger.submit_implementation_contract(proposer='r2',node_id='b',plan_version=2,goal='Implement B',must_preserve_invariants=['preserve outputs'],forbidden_behaviors=['no reruns'],acceptance_conditions=['bounded result'])
    monkeypatch.setenv('DANUS_PROJECT_DIR',str(p))
    monkeypatch.setenv('DANUS_AUTHOR','v')
    monkeypatch.setenv('DANUS_ROLE','reviewer')
    monkeypatch.setenv('DANUS_ASSIGNMENT_ID',ledger.current('v')['assignment_id'])
    with pytest.raises(ValueError, match='subject'):
        gateway.implementation_review_contract(contract['contract_id'],'APPROVE','looks valid from old context')
    assert ledger.implementation_contract(contract['contract_id'])['status']=='PROPOSED'
    worker(p,'e','experimenter')
    monkeypatch.setattr(cli,'_start_one',lambda wl:'started')
    report=cli.do_supervise('P',apply=True)
    assert ledger.current('e') is None, {'experimenter':ledger.current('e'),'applied':report['autonomy_applied']}


def _worker_run(wdir, crash_before_done=False):
    from danus.execution import loop
    if crash_before_done:
        update=ProgressLedger.update
        def interrupted(self, **kwargs):
            if kwargs.get('outcome')=='COMPLETE':
                os._exit(73)
            return update(self,**kwargs)
        ProgressLedger.update=interrupted
    raise SystemExit(loop.main(str(wdir)))


def fake_backend(tmp_path,monkeypatch):
    counter=tmp_path/'executions.txt'
    backend=tmp_path/'fake-codex'
    backend.write_text('#!/usr/bin/python3\nimport json, os\nfrom pathlib import Path\np=Path(os.environ["AUDIT_COUNTER"])\nwith p.open("a") as f: f.write("executed\\n")\nprint(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"bounded operation finished\\nDANUS_OUTCOME: COMPLETE"}}))\n',encoding='utf-8')
    backend.chmod(0o755)
    monkeypatch.setenv('DANUS_CODEX_BIN',str(backend))
    monkeypatch.setenv('AUDIT_COUNTER',str(counter))
    monkeypatch.setenv('CODEX_BACKEND','api')
    monkeypatch.setenv('DANUS_MAX_ROUNDS','1')
    monkeypatch.setenv('DANUS_ROUND_BEAT','0')
    return counter


def test_A07_checkpointed_complete_must_not_execute_twice_after_crash(tmp_path,monkeypatch):
    from danus.execution import context as C
    p, graph, ledger=project(tmp_path,monkeypatch)
    w=worker(p)
    cli.do_assign('P/r','append one experiment result',node_id='root')
    counter=fake_backend(tmp_path,monkeypatch)
    ctx=mp.get_context('fork')
    first=ctx.Process(target=_worker_run,args=(w,True))
    first.start(); first.join(10)
    if first.is_alive():
        first.kill(); first.join()
    assert first.exitcode==73
    checkpoint=C.checkpoint_for_prompt(w,'researcher')
    assert checkpoint and 'DANUS_OUTCOME: COMPLETE' in checkpoint['last_agent_message']
    assert ledger.current('r')['state']=='RUNNING'
    second=ctx.Process(target=_worker_run,args=(w,False))
    second.start(); second.join(10)
    if second.is_alive():
        second.kill(); second.join()
    assert second.exitcode==0
    assert counter.read_text().splitlines()==['executed'], counter.read_text()


def test_A08_later_commit_must_not_erase_crashed_commit_task_recovery(tmp_path,monkeypatch):
    from danus.orchestration import planning
    p, graph, ledger=project(tmp_path,monkeypatch)
    worker(p)
    worker(p,'v','reviewer')
    cli.do_assign('P/r','produce bounded proposal',node_id='root')
    done(ledger)
    proposal={'assignment_id':ledger.current('r')['assignment_id'],'base_plan_version':1,'reason':'bounded route','operations':[{'op':'add_node','node_id':'a','title':'A','parent':'root'}],'tasks':[{'node_id':'a','role':'researcher','task':'perform A'}]}
    path=planning._proposal_path(p)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(proposal),encoding='utf-8')
    captured=planning._capture_proposal('P')
    assert captured['result']=='captured'
    cid=captured['change_id']
    graph.review_change(cid,reviewer='v',verdict='APPROVE',rationale='approved A')
    ctx=mp.get_context('fork')
    def crash_after_current():
        child=ResearchPlanGraph(p)
        write=child._write_json
        def interrupted(path,payload):
            if path==child._change_path(cid) and payload.get('status')=='COMMITTED':
                os._exit(74)
            return write(path,payload)
        child._write_json=interrupted
        child.commit_change(cid,committer='supervisor')
    process=ctx.Process(target=crash_after_current)
    process.start(); process.join(5)
    assert process.exitcode==74
    assert graph.snapshot()['plan_version']==2
    assert graph.get_change(cid)['status']=='APPROVED'
    commit(graph,[{'op':'add_node','node_id':'b','title':'B','parent':'root'}])
    actions=planning._recover_materialized_tasks(p)
    graph.pending_changes()
    assert (p/'tasks'/'v2'/'researcher'/'a.md').is_file(), {'status':graph.get_change(cid)['status'],'actions':actions,'historical_commit':graph.revision(2)['committed_change_id']}


@pytest.mark.parametrize('stop',['pause','deadline','tokens'])
def test_A09_route_report_must_recheck_stop_authority_under_lock(tmp_path,monkeypatch,stop):
    import time
    from danus.orchestration import supervisor_llm as S
    from danus.orchestration.tests.test_s6_scientific_route_closure import _ambiguity,_luna
    from danus.core.handoff import HandoffRequests
    monkeypatch.setenv('DANUS_AGENTS_ROOT',str(tmp_path))
    p=tmp_path/'P'
    p.mkdir()
    ledger,source,action=_ambiguity(p,source_role='experimenter')
    # One real successful reducer pass, before a later human/resource stop.
    report=cli.do_supervise('P',apply=True,token_budget=10)
    assert S._eligible(report)
    if stop=='pause':
        cli.do_pause('P')
    elif stop=='deadline':
        (p/'.run_deadline').write_text(str(time.time()-1))
    else:
        (p/'workers'/'experimenter'/'.status.json').write_text(json.dumps({'round':1,'context_tokens_total':20}))
    calls=[]
    monkeypatch.setattr(S,'_run_luna',lambda prompt:calls.append(prompt) or _luna('RETHINK'))
    final=S._route_report('P',report,10)
    routes=[h for h in HandoffRequests(ledger).all() if h['kind']=='RETHINK']
    assert not calls and not routes, {'luna_calls':len(calls),'routes':routes,'final_state':final['project_state'],'budget_stop':final.get('budget_stop')}


def test_A10_leftover_proposal_must_not_bind_to_new_assignment(tmp_path,monkeypatch):
    from danus.orchestration import planning
    p, graph, ledger=project(tmp_path,monkeypatch)
    worker(p)
    worker(p,'v','reviewer')
    old=cli.do_assign('P/r','evaluate direction A',node_id='root')
    proposal={'assignment_id':old['assignment_id'],'base_plan_version':1,'reason':'direction A result','operations':[{'op':'add_node','node_id':'a','title':'A','parent':'root'}],'tasks':[{'node_id':'a','role':'researcher','task':'perform direction A'}]}
    path=planning._proposal_path(p)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(proposal),encoding='utf-8')
    done(ledger)
    new=cli.do_assign('P/r','evaluate different direction B',node_id='root')
    done(ledger)
    captured=planning._capture_proposal('P')
    record=planning._load_proposal_record(p,captured['proposal_id']) if captured and captured.get('proposal_id') else {}
    assert captured is None or captured.get('result')=='blocked', {'old':old['assignment_id'],'new':new['assignment_id'],'captured_source':record.get('source_assignment_id'),'proposal_reason':record.get('proposal',{}).get('reason')}


def test_A11_failed_concurrent_assign_must_not_undo_successful_task(tmp_path,monkeypatch):
    p,graph,ledger=project(tmp_path,monkeypatch)
    w=worker(p)
    cli.do_assign('P/r','original task',node_id='root')
    ready=threading.Event()
    release=threading.Event()
    original=ProgressLedger.assign
    def gated(self,**kwargs):
        if kwargs['node_id']=='missing':
            ready.set()
            assert release.wait(5)
        return original(self,**kwargs)
    monkeypatch.setattr(ProgressLedger,'assign',gated)
    errors=[]
    def rejected_request():
        try:
            cli.do_assign('P/r','invalid competing task',node_id='missing')
        except SystemExit as e:
            errors.append(str(e))
    thread=threading.Thread(target=rejected_request)
    thread.start()
    assert ready.wait(5)
    accepted=cli.do_assign('P/r','accepted replacement',node_id='root')
    release.set()
    thread.join(5)
    assert errors and not thread.is_alive()
    actual=(w/'TASK.md').read_text()
    current=ledger.current('r')
    assert ProgressLedger.task_sha256(actual)==current['task_sha256'], {'accepted_id':accepted['assignment_id'],'current_id':current['assignment_id'],'actual_task':actual,'expected_task':'accepted replacement\n','rejected':errors}


def test_control_unrelated_revision_preserves_valid_assignment(tmp_path,monkeypatch):
    p,graph,ledger=project(tmp_path,monkeypatch)
    commit(graph,[{'op':'add_node','node_id':'a','title':'A','parent':'root'}])
    worker(p)
    cli.do_assign('P/r',task(p,2,'a'),node_id='a')
    before=ledger.current('r')
    commit(graph,[{'op':'add_node','node_id':'b','title':'B','parent':'root'}])
    assert not ledger._is_plan_stale(before)
    assert ledger.current('r')['assignment_id']==before['assignment_id']


def test_control_successful_completion_does_not_repeat_backend(tmp_path,monkeypatch):
    p,graph,ledger=project(tmp_path,monkeypatch)
    w=worker(p)
    cli.do_assign('P/r','append one experiment result',node_id='root')
    counter=fake_backend(tmp_path,monkeypatch)
    ctx=mp.get_context('fork')
    for _ in range(2):
        process=ctx.Process(target=_worker_run,args=(w,False))
        process.start(); process.join(10)
        if process.is_alive():
            process.kill(); process.join()
        assert process.exitcode==0
    assert ledger.current('r')['state']=='DONE'
    assert counter.read_text().splitlines()==['executed']


def test_control_observing_cancel_prevents_authority_revival(tmp_path,monkeypatch):
    p,graph,ledger=project(tmp_path,monkeypatch)
    worker(p)
    old=cli.do_assign('P/r',task(p,1,'root'),node_id='root')
    commit(graph,[{'op':'set_state','node_id':'root','state':'CANCELLED'}])
    assert ledger.current('r')['state']=='STALE'
    commit(graph,[{'op':'set_state','node_id':'root','state':'ACTIVE'}])
    with pytest.raises(ValueError,match='STALE'):
        ledger.update(worker='r',state='DONE',actor='runtime',outcome='COMPLETE',expected_assignment_id=old['assignment_id'])
