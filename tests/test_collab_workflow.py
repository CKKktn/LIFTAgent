"""Expanded collaborative review: real graphs and durable jobs, synthetic inputs only."""
from __future__ import annotations

import base64
import copy
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import threading
import time
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from PIL import Image
import pytest

from backend.db import now, uid
from backend.security import hash_password
from fake_model_provider import answer_for, json_after
from test_backend import workspace, data, first_dataset, upload, parse


@pytest.fixture
def collab_workspace(workspace, monkeypatch):
    app, client = workspace
    monkeypatch.setenv('LANGSMITH_TRACING', 'false')
    monkeypatch.setenv('LANGCHAIN_TRACING_V2', 'false')
    model = app.state.models.save({'name': 'synthetic-collab', 'model_type': 'chat',
        'base_url': 'https://synthetic.invalid/v1', 'api_key': 'synthetic-private-key', 'set_default': True})
    kb = first_dataset(client)
    docs = [upload(client, kb, name, content.encode()) for name, content in [
        ('技术协议.txt', '设备续航时间不得低于8小时。系统应支持离线缓存功能。'),
        ('项目方案.txt', '项目方案采用双电池设计，设备续航时间为10小时。该参数是方案设计，不是实测结果。'),
        ('测试记录.txt', '合成测试资料：设备实测续航时间为7.6小时，仍需核对适用条件。')]]
    parse(client, kb, docs)
    assistant = data(client.get('/api/v1/chats'))[0]
    session = data(client.post(f"/api/v1/chats/{assistant['id']}/sessions", json={'name': '合成协审会话'}))
    control = {'overrides': {}, 'hold': None, 'entered': threading.Event(), 'release': threading.Event()}
    control['release'].set()
    calls = []

    def respond(prompt):
        messages = [{'role': {'human': 'user', 'ai': 'assistant'}.get(item.type, item.type), 'content': item.content}
            for item in prompt.to_messages()]
        combined = '\n'.join(str(item['content']) for item in messages)
        marker = next((name for name in ('COLLAB_REWRITE_V1', 'COLLAB_EVIDENCE_V1', 'COLLAB_VALIDATE_V1', 'COLLAB_ANSWER_V1') if name in combined), 'OTHER')
        payload = json_after(str(messages[-1]['content']), 'INPUT_JSON:') if 'INPUT_JSON:' in str(messages[-1]['content']) else {}
        calls.append((marker, copy.deepcopy(payload)))
        if control['hold'] == marker:
            control['entered'].set()
            if not control['release'].wait(15):
                raise TimeoutError('Synthetic model gate timed out')
        override = control['overrides'].get(marker)
        answer = override(payload) if callable(override) else override
        if answer is None:
            answer = answer_for(messages)
        return AIMessage(content=answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False))

    monkeypatch.setattr(app.state.models, 'chat', lambda *args, **kwargs: RunnableLambda(respond))
    value = SimpleNamespace(app=app, client=client, models=app.state.models, kb=kb, docs=docs,
        model=model, assistant=assistant, session=session, calls=calls, control=control,
        owner=app.state.db.one('SELECT id FROM users')['id'])
    try:
        yield value
    finally:
        control['release'].set()


def create(w, *, question='设备续航时间是多少？', **options):
    response = w.client.post('/api/v1/collab-jobs', json={'chat_id': w.assistant['id'],
        'session_id': w.session['id'], 'question': question, **options})
    assert response.status_code == 200, response.text
    return response.json()


def wait_job(w, identifier, statuses=('completed', 'waiting_confirmation', 'failed', 'stale', 'cancelled'), timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = w.client.get('/api/v1/collab-jobs/' + identifier)
        assert response.status_code == 200, response.text
        row = response.json()
        if row['status'] in statuses:
            return row
        time.sleep(.025)
    pytest.fail('Synthetic collab job did not reach expected status: ' + str(row))


def act(w, job, action, **body):
    return w.client.post(f"/api/v1/collab-jobs/{job['id']}/{action}", json={'revision': job['revision'], **body})


def messages(w, session_id=None):
    response = w.client.get(f"/api/v1/chats/{w.assistant['id']}/sessions/{session_id or w.session['id']}")
    return data(response)['messages']


def waiting(w):
    job = wait_job(w, create(w, require_confirmation=True)['id'])
    assert job['status'] == 'waiting_confirmation', job
    return job


def test_image_message_reaches_model_without_knowledge_hits_and_is_saved(collab_workspace, monkeypatch):
    w = collab_workspace
    w.app.state.db.json_save("chats", {**w.assistant, "dataset_ids": []})
    buffer = BytesIO()
    Image.new("RGB", (40, 20), "white").save(buffer, format="PNG")
    picture = buffer.getvalue()
    encoded = base64.b64encode(picture).decode("ascii")
    received = []

    def respond(prompt):
        received.extend(prompt.to_messages()[-1].content)
        return AIMessage(content="【附件图】合成尺寸标注为 50 mm，请核对原图。")

    monkeypatch.setattr(w.models, "chat", lambda *args, **kwargs: RunnableLambda(respond))
    args = {"image": {"name": "drawing.png", "data": encoded}, "request_id": "image-upload"}
    job = wait_job(w, create(w, question="请描述图片", **args)["id"])
    assert job["status"] == "waiting_confirmation", job
    assert job["validation"]["passed"] and "50 mm" in job["answer"]
    assert any(block.get("image_url", {}).get("url") == "data:image/png;base64," + encoded for block in received if isinstance(block, dict))
    saved = messages(w)
    assert saved[0]["image"] == job["image"]
    url = "/api/v1/collab-jobs/images/" + saved[0]["image"]["id"]
    response = w.client.get(url)
    assert response.headers["content-type"] == "image/png" and response.content == picture
    assert TestClient(w.app).get(url).status_code == 401
    assert create(w, question="请描述图片", **args)["id"] == job["id"]
    assert len(messages(w)) == 2
    conflict = w.client.post("/api/v1/collab-jobs", json={
        "chat_id": w.assistant["id"], "session_id": w.session["id"], "question": "请描述图片",
        "request_id": args["request_id"],
    })
    assert conflict.status_code == 409
    assert act(w, job, "cancel").status_code == 200
    deleted = w.client.request("DELETE", f"/api/v1/chats/{w.assistant['id']}/sessions", json={"ids": [w.session["id"]]})
    assert deleted.status_code == 200 and w.client.get(url).status_code == 404


def test_invalid_collab_image_does_not_create_a_turn(collab_workspace):
    w = collab_workspace
    response = w.client.post("/api/v1/collab-jobs", json={
        "chat_id": w.assistant["id"], "session_id": w.session["id"], "question": "请看图",
        "image": {"name": "drawing.png", "data": base64.b64encode(b"<svg/>").decode("ascii")},
    })
    assert response.status_code == 400
    assert messages(w) == []


def test_factual_question_runs_real_graph_and_saves_one_turn(collab_workspace):
    w = collab_workspace
    job = wait_job(w, create(w)['id'])
    assert job['status'] == 'completed', job
    assert '10小时' in job['answer'] and job['reference']['chunks']
    assert job['validation']['passed']
    assert {'COLLAB_EVIDENCE_V1', 'COLLAB_ANSWER_V1', 'COLLAB_VALIDATE_V1'} <= {name for name, _ in w.calls}
    assert 1 <= job['retrieval_rounds'] <= 3
    saved = messages(w)
    assert [row['role'] for row in saved] == ['user', 'assistant']
    assert saved[1]['replyState'] == 'complete'
    assert saved[1]['content'] == job['answer']
    assert len({row['id'] for row in saved}) == 2
    assert job['trace']


def test_followup_is_rewritten_using_saved_history(collab_workspace):
    w = collab_workspace
    first = wait_job(w, create(w)['id'])
    assert first['status'] == 'completed'
    second = wait_job(w, create(w, question='那实际测试结果呢？')['id'])
    assert '续航' in second['standalone_question']
    assert second['standalone_question'] != '那实际测试结果呢？'
    rewrite_calls = [payload for name, payload in w.calls if name == 'COLLAB_REWRITE_V1']
    assert rewrite_calls and any(row.get('role') == 'user' and '续航' in row.get('content', '') for row in rewrite_calls[-1]['history'])
    assert len(messages(w)) == 4


def test_verdict_question_requires_real_human_interrupt(collab_workspace):
    w = collab_workspace
    job = wait_job(w, create(w, question='设备的续航是否达标？')['id'])
    assert job['status'] == 'waiting_confirmation'
    assert job['requires_confirmation']
    assert messages(w)[1]['replyState'] == 'waiting_confirmation'
    snapshot = w.app.state.collab_jobs.graph.get_state({'configurable': {'thread_id': job['id']}})
    assert any(task.interrupts for task in snapshot.tasks)


@pytest.mark.parametrize('decision', ['approve', 'revise', 'reject'])
def test_human_decision_is_audited_without_duplicate_turn_or_model_call(collab_workspace, decision):
    w = collab_workspace
    job = waiting(w)
    calls = len(w.calls)
    edited = '人工核对：项目方案续航10小时 [1]，实测材料应另行核实。'
    response = act(w, job, 'review', decision=decision, answer=edited if decision == 'revise' else '', notes='合成人工意见')
    assert response.status_code == 200, response.text
    final = wait_job(w, job['id'], statuses=('completed', 'cancelled', 'failed'))
    assert final['status'] == 'completed', final
    assert len(w.calls) == calls
    assert len(messages(w)) == 2
    if decision == 'revise':
        assert final['answer'] == edited
    if decision == 'reject':
        assert '退回' in final['answer'] or '拒绝' in final['answer']
    rows = w.app.state.db.rows('SELECT * FROM collab_review_decisions WHERE job_id=?', (job['id'],))
    assert len(rows) == 1 and decision in json.dumps(rows, ensure_ascii=False)
    assert '合成人工意见' in json.dumps(rows, ensure_ascii=False)


def test_invalid_human_source_or_stale_revision_does_not_consume_interrupt(collab_workspace):
    w = collab_workspace
    job = waiting(w)
    response = act(w, job, 'review', decision='revise', answer='根据不存在的来源 [999]，已符合。', notes='合成')
    assert response.status_code == 400, response.text
    response = w.client.post(f"/api/v1/collab-jobs/{job['id']}/review", json={'revision': job['revision'] - 1, 'decision': 'approve'})
    assert response.status_code == 409
    assert wait_job(w, job['id'])['status'] == 'waiting_confirmation'
    assert not w.app.state.db.rows('SELECT * FROM collab_review_decisions WHERE job_id=?', (job['id'],))


def test_explicit_human_supplement_runs_retrieval_again_with_limited_budget(collab_workspace):
    w = collab_workspace
    job = waiting(w)
    first_rounds = job['retrieval_rounds']
    response = act(w, job, 'review', decision='supplement', query='设备实测续航时间7.6小时的测试记录', notes='补充检索测试记录')
    assert response.status_code == 200, response.text
    result = wait_job(w, job['id'])
    assert result['status'] == 'waiting_confirmation'
    assert result['retrieval_rounds'] > first_rounds
    assert result['manual_rounds'] == 1
    assert len(messages(w)) == 2
    assert len({(item.get('document_id'), item.get('id'), item.get('content')) for item in result['reference']['chunks']}) == len(result['reference']['chunks'])


def test_insufficient_evidence_has_bounded_retrieval_and_no_false_completion(collab_workspace):
    w = collab_workspace
    w.control['overrides']['COLLAB_EVIDENCE_V1'] = {'sufficient': False, 'relevant_ids': [], 'missing': ['缺少对应对象的实测材料'], 'conflicts': [], 'summary': '材料不足'}
    job = wait_job(w, create(w, question='NO_SOURCE_TEST 对应设备的实测是否合格？')['id'])
    assert job['status'] == 'waiting_confirmation'
    assert job['retrieval_rounds'] <= 3
    assert not job['evidence_assessment']['sufficient']
    assert job['evidence_assessment']['missing']
    assert '不足' in job['answer']
    assert sum(name == 'COLLAB_EVIDENCE_V1' for name, _ in w.calls) <= 3
    assert len(messages(w)) == 2


@pytest.mark.parametrize('answer', [
    '设备续航为10小时 [999]。',
    '设备续航为99小时 [1]。',
    '方案续航10分钟 [1]。'])
def test_program_checks_reject_bad_citations_numbers_and_units_even_if_model_approves(collab_workspace, answer):
    w = collab_workspace
    w.control['overrides']['COLLAB_ANSWER_V1'] = answer
    w.control['overrides']['COLLAB_VALIDATE_V1'] = {'grounded': True, 'issues': [], 'supported_ids': [1], 'summary': '合成模型故意忽略错误'}
    job = wait_job(w, create(w)['id'])
    assert job['status'] == 'waiting_confirmation', job
    assert not job['validation']['passed'] and job['validation']['issues']
    assert sum(name == 'COLLAB_ANSWER_V1' for name, _ in w.calls) <= 2


def test_same_session_rejects_second_job_and_retry_key_is_idempotent(collab_workspace):
    w = collab_workspace
    first = create(w, require_confirmation=True, request_id='synthetic-request-1')
    job = wait_job(w, first['id'])
    duplicate = create(w, require_confirmation=True, request_id='synthetic-request-1')
    assert duplicate['id'] == first['id']
    response = w.client.post('/api/v1/collab-jobs', json={'chat_id': w.assistant['id'], 'session_id': w.session['id'], 'question': '另一条问题'})
    assert response.status_code == 409
    assert len(messages(w)) == 2
    assert len(w.app.state.db.rows('SELECT id FROM collab_jobs')) == 1
    assert job['status'] == 'waiting_confirmation'


@pytest.mark.parametrize('payload', [
    {'question': ''}, {'question': 'x' * 16001}, {'require_confirmation': 'false'}, {'session_id': 'missing-session'}])
def test_invalid_create_has_no_new_job_or_message(collab_workspace, payload):
    w = collab_workspace
    before = w.app.state.db.rows('SELECT * FROM sessions')
    response = w.client.post('/api/v1/collab-jobs', json={'chat_id': w.assistant['id'], 'session_id': w.session['id'], 'question': '设备续航时间是多少？', **payload})
    assert response.status_code in (400, 404, 422), response.text
    assert not w.app.state.db.rows('SELECT id FROM collab_jobs')
    assert w.app.state.db.rows('SELECT * FROM sessions') == before


def test_other_owner_cannot_read_mutate_or_list_task(collab_workspace):
    w = collab_workspace
    job = waiting(w)
    owner = uid()
    w.app.state.db.execute('INSERT INTO users(id,username,password_hash,created_at) VALUES (?,?,?,?)',
        (owner, 'other-user', hash_password('other-test-only-password'), now()))
    other = TestClient(w.app)
    try:
        assert other.post('/auth/login', json={'username': 'other-user', 'password': 'other-test-only-password'}).status_code == 200
        assert other.get('/api/v1/collab-jobs/' + job['id']).status_code == 404
        assert other.post(f"/api/v1/collab-jobs/{job['id']}/review", json={'revision': job['revision'], 'decision': 'approve'}).status_code == 404
        assert other.get('/api/v1/collab-jobs').json() == []
    finally:
        other.close()


@pytest.mark.parametrize('change', ['chunk', 'model', 'assistant'])
def test_material_model_or_assistant_change_blocks_old_draft_confirmation(collab_workspace, change):
    w = collab_workspace
    job = waiting(w)
    if change == 'chunk':
        w.app.state.db.execute("UPDATE chunks SET content=content || ' 材料版本已修改' WHERE document_id=?", (w.docs[1]['id'],))
    elif change == 'model':
        w.models.save({**w.model, 'api_key': 'different-synthetic-key'})
    else:
        data(w.client.patch('/api/v1/chats/' + w.assistant['id'], json={'dataset_ids': []}))
    response = act(w, job, 'review', decision='approve')
    assert response.status_code == 409, response.text
    assert wait_job(w, job['id'])['status'] == 'stale'
    assert not w.app.state.db.rows('SELECT * FROM collab_review_decisions WHERE job_id=?', (job['id'],))


def test_stop_resume_keeps_one_turn_and_durable_progress(collab_workspace):
    w = collab_workspace
    w.control['hold'] = 'COLLAB_ANSWER_V1'
    w.control['release'].clear()
    created = create(w)
    assert w.control['entered'].wait(6)
    current = w.client.get('/api/v1/collab-jobs/' + created['id']).json()
    response = act(w, current, 'stop')
    assert response.status_code == 200, response.text
    w.control['release'].set()
    stopped = wait_job(w, created['id'], statuses=('stopped', 'completed', 'failed'))
    assert stopped['status'] == 'stopped' and stopped['can_resume'], stopped
    before = messages(w)
    assert len(before) == 2
    response = act(w, stopped, 'resume')
    assert response.status_code == 200, response.text
    result = wait_job(w, created['id'])
    assert result['status'] == 'completed', result
    assert [item['id'] for item in messages(w)] == [item['id'] for item in before]


def test_cancel_waiting_draft_allows_next_turn_and_delete_is_guarded(collab_workspace):
    w = collab_workspace
    job = waiting(w)
    path = f"/api/v1/chats/{w.assistant['id']}/sessions"
    assert w.client.request('DELETE', path, json={'ids': [w.session['id']]}).status_code == 409
    response = act(w, job, 'cancel')
    assert response.status_code == 200, response.text
    assert wait_job(w, job['id'])['status'] == 'cancelled'
    assert messages(w)[1]['replyState'] == 'cancelled'
    result = wait_job(w, create(w)['id'])
    assert result['status'] == 'completed'
    assert len(messages(w)) == 4
    assert w.client.request('DELETE', path, json={'ids': [w.session['id']]}).status_code == 200


def test_sse_is_observation_only_and_public_output_does_not_expose_secrets(collab_workspace):
    w = collab_workspace
    job = waiting(w)
    before_calls = len(w.calls)
    response = w.client.get(f"/api/v1/collab-jobs/{job['id']}/events?after=0")
    assert response.status_code == 200 and 'text/event-stream' in response.headers['content-type']
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    assert events and events[-1]['job']['status'] == 'waiting_confirmation'
    assert all(event['type'] == 'state' for event in events)
    assert len(w.calls) == before_calls
    public = response.text + w.client.get('/api/v1/collab-jobs').text
    assert 'synthetic-private-key' not in public and '"api_key"' not in public
    assert len(messages(w)) == 2
    for path in w.app.state.settings.data_dir.glob('collab-checkpoints.sqlite3*'):
        assert b'synthetic-private-key' not in path.read_bytes()


def wait_service(service, identifier, owner, statuses, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = service.get(identifier, owner)
        if row['status'] in statuses:
            return row
        time.sleep(.025)
    pytest.fail('Restored synthetic job did not reach expected state: ' + str(row))


def settled(service):
    deadline = time.monotonic() + 5
    while any(not future.done() for future in list(service._futures.values())):
        assert time.monotonic() < deadline, 'Synthetic worker did not settle'
        time.sleep(.02)


def test_waiting_checkpoint_survives_service_recreation_without_model_calls(collab_workspace):
    from backend.collab_jobs import CollabJobService
    w = collab_workspace
    job = waiting(w)
    old = w.app.state.collab_jobs
    settled(old)
    old.close()
    calls = len(w.calls)
    restored = CollabJobService(w.app.state.db, w.models, w.app.state.knowledge, w.app.state.retrieval, w.app.state.settings)
    try:
        assert len(w.calls) == calls
        current = restored.get(job['id'], w.owner)
        assert current['status'] == 'waiting_confirmation'
        restored.review(job['id'], w.owner, {'revision': current['revision'], 'decision': 'approve'})
        final = wait_service(restored, job['id'], w.owner, ('completed', 'failed'))
        assert final['status'] == 'completed'
        assert len(w.calls) == calls and len(messages(w)) == 2
    finally:
        settled(restored)
        restored.close()


def test_committed_human_decision_replays_after_crash_before_graph_resume(collab_workspace, monkeypatch):
    from backend.collab_jobs import CollabJobService
    w = collab_workspace
    job = waiting(w)
    old = w.app.state.collab_jobs
    settled(old)
    submitted = []
    monkeypatch.setattr(old, '_submit', lambda identifier, command: submitted.append((identifier, command)))
    response = act(w, job, 'review', decision='revise', answer='人工修订：方案续航10小时 [1]，实测应另行核对。', notes='已核对')
    assert response.status_code == 200 and submitted
    calls = len(w.calls)
    old.close()
    restored = CollabJobService(w.app.state.db, w.models, w.app.state.knowledge, w.app.state.retrieval, w.app.state.settings)
    try:
        current = restored.get(job['id'], w.owner)
        assert current['status'] == 'stopped' and len(w.calls) == calls
        restored.resume(job['id'], w.owner, {'revision': current['revision']})
        final = wait_service(restored, job['id'], w.owner, ('completed', 'waiting_confirmation', 'failed'))
        assert final['status'] == 'completed', final
        assert final['answer'] == '人工修订：方案续航10小时 [1]，实测应另行核对。'
        assert len(w.calls) == calls and len(messages(w)) == 2
        assert len(w.app.state.db.rows('SELECT id FROM collab_review_decisions WHERE job_id=?', (job['id'],))) == 1
    finally:
        settled(restored)
        restored.close()


def test_existing_retry_key_with_invalid_question_is_a_client_error(collab_workspace):
    w = collab_workspace
    job = wait_job(w, create(w, require_confirmation=True, request_id='audit-retry')['id'])
    response = w.client.post('/api/v1/collab-jobs', json={'chat_id': w.assistant['id'], 'session_id': w.session['id'], 'request_id': 'audit-retry', 'question': ['not text']})
    assert response.status_code == 400
    assert len(messages(w)) == 2 and job['status'] == 'waiting_confirmation'


def test_invalid_new_session_request_does_not_leave_an_empty_session(collab_workspace):
    w = collab_workspace
    previous = w.app.state.db.rows('SELECT * FROM sessions')
    response = w.client.post('/api/v1/collab-jobs', json={'chat_id': w.assistant['id'], 'question': '设备续航时间是多少？', 'require_confirmation': 'invalid'})
    assert response.status_code == 400
    assert w.app.state.db.rows('SELECT * FROM sessions') == previous
    assert not w.app.state.db.rows('SELECT id FROM collab_jobs')


def test_rewrite_cannot_invent_parameters_outside_question_and_history(collab_workspace):
    w = collab_workspace
    assert wait_job(w, create(w)['id'])['status'] == 'completed'
    w.control['overrides']['COLLAB_REWRITE_V1'] = {'standalone_question': '该设备在99小时测试后是否符合？', 'intent': 'review'}
    job = wait_job(w, create(w, question='那实际测试结果呢？')['id'])
    assert job['status'] == 'failed'
    assert len(messages(w)) == 4
    assert '99小时' not in job.get('standalone_question', '')


@pytest.mark.parametrize('state', [
    {'manual_rounds': 2, 'retrieval_rounds': 3},
    {'manual_rounds': 1, 'retrieval_rounds': 5}])
def test_explicit_human_supplement_cannot_escape_global_budget(state):
    from backend.collab_graph import validate_review
    with pytest.raises(HTTPException) as error:
        validate_review({**state, 'dataset_ids': ['synthetic-kb'], 'references': []}, {'decision': 'supplement', 'query': '继续无限检索'})
    assert error.value.status_code == 400


def test_broken_structured_output_fails_with_bounded_attempts(collab_workspace):
    w = collab_workspace
    w.control['overrides']['COLLAB_EVIDENCE_V1'] = 'not JSON at all'
    job = wait_job(w, create(w)['id'])
    assert job['status'] == 'failed'
    assert sum(name == 'COLLAB_EVIDENCE_V1' for name, _ in w.calls) == 2
    assert not any(name == 'COLLAB_ANSWER_V1' for name, _ in w.calls)
    assert len(messages(w)) == 2


def test_invalid_citation_cannot_be_approved_without_revision(collab_workspace):
    w = collab_workspace
    w.control['overrides']['COLLAB_ANSWER_V1'] = '设备续航10小时，依据不存在的来源 [999]。'
    w.control['overrides']['COLLAB_VALIDATE_V1'] = {'grounded': True, 'issues': [], 'supported_ids': [1], 'summary': '故意忽略错误的合成校验'}
    job = wait_job(w, create(w)['id'])
    assert job['status'] == 'waiting_confirmation'
    response = act(w, job, 'review', decision='approve', notes='直接确认无效来源')
    assert response.status_code == 400, response.text
    assert wait_job(w, job['id'])['status'] == 'waiting_confirmation'
    assert not w.app.state.db.rows('SELECT id FROM collab_review_decisions WHERE job_id=?', (job['id'],))


@pytest.mark.parametrize('answer', ['设备实测续航7.6小时 [1]。', '设备已实测合格，续航10小时 [1]。'])
def test_quantities_and_measurement_claims_must_match_the_cited_source(collab_workspace, answer):
    w = collab_workspace
    # Control source order so the plan cannot borrow the test record's facts.
    plan, test = w.docs[1], w.docs[2]
    w.app.state.retrieval.search = lambda options: {'chunks': [
        {'id': 'plan-chunk', 'document_id': plan['id'], 'document_name': plan['name'], 'dataset_id': w.kb,
         'content': '项目方案设计续航10小时，不是实测结果。'},
        {'id': 'test-chunk', 'document_id': test['id'], 'document_name': test['name'], 'dataset_id': w.kb,
         'content': '设备实测续航7.6小时，需核对适用条件。'}]}
    w.control['overrides']['COLLAB_ANSWER_V1'] = answer
    w.control['overrides']['COLLAB_VALIDATE_V1'] = {'grounded': True, 'issues': [], 'supported_ids': [1], 'summary': '故意忽略归属错误'}
    job = wait_job(w, create(w)['id'])
    assert job['status'] == 'waiting_confirmation'
    assert not job['validation']['passed']
    assert any('来源' in issue or '实测' in issue for issue in job['validation']['issues'])


def test_review_actor_and_time_are_bound_by_server_not_browser(collab_workspace):
    w = collab_workspace
    job = waiting(w)
    response = act(w, job, 'review', decision='approve', notes='真实合成用户提交', actor_id='forged-other-user', reviewed_at='forged-time')
    assert response.status_code == 200, response.text
    final = wait_job(w, job['id'])
    assert final['status'] == 'completed'
    assert final['review']['actor_id'] == w.owner
    assert final['review']['reviewed_at'] != 'forged-time'
    row = w.app.state.db.one('SELECT * FROM collab_review_decisions WHERE job_id=?', (job['id'],))
    assert row['owner_id'] == w.owner


def test_old_blocked_worker_cannot_overwrite_new_service_result_or_checkpoint(collab_workspace):
    from backend.collab_jobs import CollabJobService
    w = collab_workspace
    w.control['hold'] = 'COLLAB_EVIDENCE_V1'
    w.control['release'].clear()
    first = create(w)
    assert w.control['entered'].wait(6)
    old = w.app.state.collab_jobs
    # Only the old call stays blocked; the restored worker is allowed to run.
    w.control['hold'] = None
    old.close()
    restored = CollabJobService(w.app.state.db, w.models, w.app.state.knowledge, w.app.state.retrieval, w.app.state.settings)
    try:
        stopped = restored.get(first['id'], w.owner)
        assert stopped['status'] == 'stopped'
        restored.resume(first['id'], w.owner, {'revision': stopped['revision']})
        final = wait_service(restored, first['id'], w.owner, ('completed', 'failed', 'waiting_confirmation'))
        assert final['status'] == 'completed', final
        before = messages(w)
        w.control['release'].set()
        settled(old)
        # Allow final checkpoint writes/callbacks to finish before comparison.
        time.sleep(.08)
        assert restored.get(first['id'], w.owner)['status'] == 'completed'
        assert messages(w) == before
        snapshot = restored.graph.get_state(restored._config(first['id']))
        assert not snapshot.next, 'Late old node replaced the completed graph checkpoint'
        assert snapshot.values['final_answer'] == final['answer']
    finally:
        w.control['release'].set()
        settled(old)
        settled(restored)
        restored.close()


def test_pause_at_human_confirmation_resumes_same_interrupt_without_model_calls(collab_workspace):
    w = collab_workspace
    job = waiting(w)
    calls = len(w.calls)
    stopped_response = act(w, job, 'stop')
    assert stopped_response.status_code == 200
    stopped = wait_job(w, job['id'], statuses=('stopped',))
    assert act(w, stopped, 'resume').status_code == 200
    resumed = wait_job(w, job['id'])
    assert resumed['status'] == 'waiting_confirmation'
    assert resumed['answer'] == job['answer']
    assert len(w.calls) == calls and len(messages(w)) == 2


def test_full_workflow_never_exceeds_three_auto_and_two_human_retrievals(collab_workspace):
    w = collab_workspace
    w.control['overrides']['COLLAB_EVIDENCE_V1'] = {'sufficient': False, 'relevant_ids': [], 'missing': ['缺少特定批次的测量记录'], 'conflicts': [], 'summary': '合成材料缺口'}
    job = wait_job(w, create(w, require_confirmation=True)['id'])
    assert job['status'] == 'waiting_confirmation' and job['retrieval_rounds'] == 3
    for index in range(2):
        response = act(w, job, 'review', decision='supplement', query='核对该设备批次的原始测量资料', notes='合成人工补充')
        assert response.status_code == 200, response.text
        job = wait_job(w, job['id'])
        assert job['status'] == 'waiting_confirmation' and job['manual_rounds'] == index + 1
    assert job['retrieval_rounds'] == 5
    response = act(w, job, 'review', decision='supplement', query='不得继续无限检索')
    assert response.status_code == 400
    assert len(w.app.state.db.rows('SELECT id FROM collab_review_decisions WHERE job_id=?', (job['id'],))) == 2
    assert len(messages(w)) == 2


def test_failed_node_can_explicitly_resume_after_fix_without_new_turn(collab_workspace):
    w = collab_workspace
    w.control['overrides']['COLLAB_EVIDENCE_V1'] = 'not JSON'
    job = wait_job(w, create(w)['id'])
    assert job['status'] == 'failed' and job['can_resume']
    original_ids = [message['id'] for message in messages(w)]
    w.control['overrides'].clear()
    response = act(w, job, 'resume')
    assert response.status_code == 200, response.text
    final = wait_job(w, job['id'])
    assert final['status'] == 'completed'
    assert [message['id'] for message in messages(w)] == original_ids


@pytest.mark.parametrize('payload', [{'chat_id': []}, {'session_id': []}, {'model_id': {}}, {'llm_id': []}])
def test_non_scalar_identifiers_are_client_errors_and_atomic(collab_workspace, payload):
    w = collab_workspace
    previous = w.app.state.db.rows('SELECT * FROM sessions')
    response = w.client.post('/api/v1/collab-jobs', json={'chat_id': w.assistant['id'], 'session_id': w.session['id'], 'question': '设备续航时间是多少？', **payload})
    assert response.status_code == 400, response.text
    assert w.app.state.db.rows('SELECT * FROM sessions') == previous
    assert not w.app.state.db.rows('SELECT id FROM collab_jobs')


def test_idempotency_key_cannot_change_human_confirmation_requirement(collab_workspace):
    w = collab_workspace
    job = wait_job(w, create(w, require_confirmation=True, request_id='same-command')['id'])
    response = w.client.post('/api/v1/collab-jobs', json={'chat_id': w.assistant['id'], 'session_id': w.session['id'], 'question': '设备续航时间是多少？', 'require_confirmation': False, 'request_id': 'same-command'})
    assert response.status_code == 409
    assert len(messages(w)) == 2 and job['status'] == 'waiting_confirmation'


def test_history_scope_filters_apply_before_global_recent_limit(collab_workspace):
    w = collab_workspace
    job = waiting(w)
    other_chat = data(w.client.post('/api/v1/chats', json={'name': '另一合成助手'}))
    other_session = data(w.client.post(f"/api/v1/chats/{other_chat['id']}/sessions", json={'name': '其他历史'}))
    template = w.app.state.db.one('SELECT * FROM collab_jobs WHERE id=?', (job['id'],))
    with w.app.state.db.transaction() as connection:
        for index in range(201):
            record = {**template, 'id': uid(), 'session_id': other_session['id'], 'chat_id': other_chat['id'],
                'status': 'completed', 'request_id': None, 'updated_at': f'2099-01-01T00:00:{index:03d}+00:00'}
            connection.execute('INSERT INTO collab_jobs(' + ','.join(record) + ') VALUES (' + ','.join('?' for _ in record) + ')', tuple(record.values()))
    for field, identifier in [('session_id', w.session['id']), ('chat_id', w.assistant['id'])]:
        response = w.client.get('/api/v1/collab-jobs', params={field: identifier})
        assert response.status_code == 200
        assert [row['id'] for row in response.json()] == [job['id']]


def test_positive_stale_stop_is_safe_but_human_approval_requires_exact_revision(collab_workspace):
    w = collab_workspace
    job = waiting(w)
    w.app.state.collab_jobs._update(job['id'], node='synthetic_progress_tick', message='合成状态变化')
    response = act(w, job, 'review', decision='approve')
    assert response.status_code == 409
    response = act(w, job, 'stop')
    assert response.status_code == 200, response.text
    assert wait_job(w, job['id'], statuses=('stopped',))['status'] == 'stopped'


@pytest.mark.parametrize('same_request', [False, True])
def test_concurrent_starts_are_atomic_and_same_request_is_exactly_once(collab_workspace, same_request):
    w = collab_workspace
    barrier = threading.Barrier(3)
    def start(index):
        barrier.wait(timeout=5)
        return w.client.post('/api/v1/collab-jobs', json={'chat_id': w.assistant['id'], 'session_id': w.session['id'],
            'question': '设备续航时间是多少？', 'require_confirmation': True,
            'request_id': 'one-command' if same_request else 'command-' + str(index)})
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(start, index) for index in range(2)]
        barrier.wait(timeout=5)
        responses = [future.result(timeout=10) for future in futures]
    assert sorted(response.status_code for response in responses) == ([200, 200] if same_request else [200, 409])
    accepted = [response.json() for response in responses if response.status_code == 200]
    assert len({row['id'] for row in accepted}) == 1
    assert wait_job(w, accepted[0]['id'])['status'] == 'waiting_confirmation'
    assert len(messages(w)) == 2
    assert len(w.app.state.db.rows('SELECT id FROM collab_jobs')) == 1


def test_concurrent_human_approvals_commit_one_audit_record(collab_workspace):
    w = collab_workspace
    job = waiting(w)
    barrier = threading.Barrier(3)
    def approve():
        barrier.wait(timeout=5)
        return act(w, job, 'review', decision='approve', notes='合成并发确认')
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(approve) for _ in range(2)]
        barrier.wait(timeout=5)
        responses = [future.result(timeout=10) for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert wait_job(w, job['id'])['status'] == 'completed'
    assert len(w.app.state.db.rows('SELECT id FROM collab_review_decisions WHERE job_id=?', (job['id'],))) == 1
    assert len(messages(w)) == 2


def test_cancelling_running_model_call_is_terminal_after_current_call_returns(collab_workspace):
    w = collab_workspace
    w.control['hold'] = 'COLLAB_ANSWER_V1'
    w.control['release'].clear()
    created = create(w)
    assert w.control['entered'].wait(6)
    current = w.client.get('/api/v1/collab-jobs/' + created['id']).json()
    response = act(w, current, 'cancel')
    assert response.status_code == 200, response.text
    w.control['release'].set()
    cancelled = wait_job(w, created['id'], statuses=('cancelled', 'failed', 'completed'))
    assert cancelled['status'] == 'cancelled'
    assert len(messages(w)) == 2 and messages(w)[1]['replyState'] == 'cancelled'
    response = act(w, cancelled, 'resume')
    assert response.status_code == 409
