"""Delivery audit: atomic writes, evidence integrity and active-session safety."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from test_backend import workspace, data, first_dataset, upload, parse
from test_review_engine import engine_workspace, extracted, prepared, force_answer
from backend.review_engine import ReviewOutputError


@pytest.mark.parametrize("base,expected_path", [
    ("https://synthetic.invalid/v1", "/v1/rerank"),
    ("https://synthetic.invalid/v1/rerank/", "/v1/rerank"),
    ("https://synthetic.invalid/compatible-api/v1", "/compatible-api/v1/reranks"),
    ("https://synthetic.invalid/compatible-api/v1/reranks", "/compatible-api/v1/reranks"),
    ("https://synthetic.invalid/compatible-api/v1/reranks/", "/compatible-api/v1/reranks"),
])
def test_rerank_connection_preserves_complete_endpoint(workspace, monkeypatch, base, expected_path):
    app, client = workspace
    model = app.state.models.save({"name": "synthetic-reranker", "model_type": "rerank", "base_url": base})
    def respond(request):
        if request.url.path != expected_path:
            return httpx.Response(404)
        payload = json.loads(request.content)
        assert payload["model"] == "synthetic-reranker"
        assert len(payload["documents"]) == 2
        return httpx.Response(200, json={"results": [
            {"index": 0, "relevance_score": .9}, {"index": 1, "relevance_score": .1}]})
    original = httpx.Client
    monkeypatch.setattr("backend.retrieval.httpx.Client",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    response = client.post("/settings/models/" + model["id"] + "/test")
    assert response.status_code == 200, response.text
    assert response.json()["success"] is True



def test_failed_upload_batch_keeps_neither_files_nor_rows(workspace):
    app, client = workspace
    kb = first_dataset(client)
    before = {path.name for path in (app.state.settings.data_dir / 'uploads').iterdir()}
    response = client.post(f'/api/v1/datasets/{kb}/documents', files=[
        ('file', ('valid.txt', b'synthetic document', 'text/plain')),
        ('file', ('empty.txt', b'', 'text/plain'))])
    assert response.status_code == 400
    assert app.state.db.one('SELECT COUNT(*) AS n FROM documents')['n'] == 0
    assert {path.name for path in (app.state.settings.data_dir / 'uploads').iterdir()} == before


def chunk_fixture(app, client):
    kb = first_dataset(client)
    document = upload(client, kb, 'synthetic.txt', b'Only synthetic audit text.')
    parse(client, kb, [document])
    chunk = app.state.db.one('SELECT * FROM chunks WHERE document_id=?', (document['id'],))
    return kb, document, chunk


def test_editing_disabled_chunk_does_not_enable_it(workspace):
    app, client = workspace
    kb, document, chunk = chunk_fixture(app, client)
    app.state.db.execute('UPDATE chunks SET enabled=0 WHERE id=?', (chunk['id'],))
    response = client.patch(f"/api/v1/datasets/{kb}/documents/{document['id']}/chunks/{chunk['id']}", json={'content': 'Revised synthetic text.'})
    assert data(response)['available'] is False


@pytest.mark.parametrize('vector', [[], [float('nan')], [float('inf')], [1, [2]], ['bad']])
def test_manual_chunk_rejects_invalid_embedding_without_writes(workspace, monkeypatch, vector):
    app, client = workspace
    kb, document, chunk = chunk_fixture(app, client)
    monkeypatch.setattr(app.state.models, 'embedding', lambda *_: ('local-features', SimpleNamespace(embed_query=lambda _: vector)))
    response = client.patch(f"/api/v1/datasets/{kb}/documents/{document['id']}/chunks/{chunk['id']}", json={'content': 'Must not be committed.'})
    assert response.status_code == 400
    assert app.state.db.one('SELECT * FROM chunks WHERE id=?', (chunk['id'],)) == chunk


def test_chunk_edit_cannot_overwrite_parser_started_during_embedding(workspace, monkeypatch):
    app, client = workspace
    kb, document, chunk = chunk_fixture(app, client)
    def embedding(_):
        app.state.db.execute("UPDATE documents SET generation=generation+1,run='RUNNING' WHERE id=?", (document['id'],))
        return [0.1, 0.2]
    monkeypatch.setattr(app.state.models, 'embedding', lambda *_: ('local-features', SimpleNamespace(embed_query=embedding)))
    response = client.patch(f"/api/v1/datasets/{kb}/documents/{document['id']}/chunks/{chunk['id']}", json={'content': 'Stale edit.'})
    assert response.status_code == 409
    assert app.state.knowledge.document(document['id'])['run'] == 'RUNNING'
    assert app.state.db.one('SELECT * FROM chunks WHERE id=?', (chunk['id'],)) == chunk


def test_deleting_chunks_while_parsing_is_rejected(workspace):
    app, client = workspace
    kb, document, chunk = chunk_fixture(app, client)
    app.state.db.execute("UPDATE documents SET run='RUNNING' WHERE id=?", (document['id'],))
    response = client.request('DELETE', f"/api/v1/datasets/{kb}/documents/{document['id']}/chunks", json={'chunk_ids': [chunk['id']]})
    assert response.status_code == 409
    assert app.state.db.one('SELECT * FROM chunks WHERE id=?', (chunk['id'],))


@pytest.mark.parametrize('embedding_id', ['local-features-v1', 'changed-model'])
def test_chunk_edit_cannot_mix_embedding_versions_or_dimensions(workspace, monkeypatch, embedding_id):
    app, client = workspace
    kb, document, chunk = chunk_fixture(app, client)
    monkeypatch.setattr(app.state.models, 'embedding', lambda *_: (embedding_id, SimpleNamespace(embed_query=lambda _: [0.1, 0.2])))
    response = client.patch(f"/api/v1/datasets/{kb}/documents/{document['id']}/chunks/{chunk['id']}", json={'content': 'Must not corrupt index.'})
    assert response.status_code == 409
    assert app.state.db.one('SELECT * FROM chunks WHERE id=?', (chunk['id'],)) == chunk


def test_per_document_parser_settings_are_used_and_invalidate_previous_index(workspace):
    app, client = workspace
    kb = first_dataset(client)
    document = upload(client, kb, 'long.txt', ('审查条件及方案材料。' * 500).encode())
    parse(client, kb, [document])
    previous = app.state.knowledge.document(document['id'])
    updated = data(client.patch(f"/api/v1/datasets/{kb}/documents/{document['id']}", json={'parser_config': {'chunk_size': 200, 'chunk_overlap': 0}}))
    assert updated['run'] == 'UNSTART'
    assert app.state.knowledge.document(document['id'])['generation'] == previous['generation'] + 1
    parse(client, kb, [document])
    chunks = app.state.db.rows('SELECT content FROM chunks WHERE document_id=?', (document['id'],))
    assert len(chunks) > previous['chunk_count']
    assert max(len(row['content']) for row in chunks) <= 200


def test_model_aliases_are_saved_as_canonical_ids(workspace):
    app, client = workspace
    model = app.state.models.save({'name': 'audit-model', 'model_type': 'chat', 'base_url': 'https://synthetic.invalid/v1'})
    app.state.models.set_default('chat', model['name'])
    assert app.state.db.one("SELECT model_id FROM model_defaults WHERE kind='chat'")['model_id'] == model['id']
    assistant = data(client.post('/api/v1/chats', json={'name': 'Audit assistant', 'llm_id': model['name']}))
    assert assistant['llm_id'] == model['id']
    assert client.delete('/settings/models/' + model['id']).status_code == 409


def test_whitespace_only_key_does_not_replace_saved_secret(workspace):
    app, _ = workspace
    model = app.state.models.save({'name': 'audit-key', 'model_type': 'chat', 'base_url': 'https://synthetic.invalid/v1', 'api_key': 'synthetic-test-only'})
    before = app.state.db.one('SELECT secret FROM models WHERE id=?', (model['id'],))
    app.state.models.save({**model, 'api_key': '   '})
    assert app.state.db.one('SELECT secret FROM models WHERE id=?', (model['id'],)) == before


@pytest.mark.parametrize('delete_assistant', [False, True])
def test_active_generation_prevents_deleting_session_or_assistant(workspace, delete_assistant):
    app, client = workspace
    assistant = data(client.post('/api/v1/chats', json={'name': 'Audit assistant'}))
    session = data(client.post(f"/api/v1/chats/{assistant['id']}/sessions", json={'name': 'In progress'}))
    lock = asyncio.Lock()
    asyncio.run(lock.acquire())
    app.state.chat.locks[session['id']] = lock
    path = f"/api/v1/chats/{assistant['id']}" + ('' if delete_assistant else '/sessions')
    response = client.request('DELETE', path, json={'ids': [session['id']]})
    assert response.status_code == 409
    assert app.state.chat.session(session['id'])
    lock.release()
    assert client.request('DELETE', path, json={'ids': [session['id']]}).status_code == 200


@pytest.mark.parametrize('kinds', [('支持', '支持'), ('背景', '背景'), ('支持', '计划')])
def test_conflict_requires_support_and_counterevidence(engine_workspace, monkeypatch, kinds):
    w = engine_workspace
    state, output = extracted(w)
    chunks = [{'reviewId': f'E{i+1}', 'document_id': w.plan['id'], 'document_name': 'Synthetic.txt', 'content': 'Synthetic evidence'} for i in range(2)]
    force_answer(w, monkeypatch, {'status': '证据冲突', 'analysis': '需要比较来源E1和E2', 'evidence': [
        {'source_id': f'E{i+1}', 'kind': kind, 'analysis': '材料性质'} for i, kind in enumerate(kinds)]})
    assert w.engine.judge(state['report'], output['requirements'][0], chunks)['status'] == '待人工复核'


@pytest.mark.parametrize('reference', ['E99', 'E2'])
def test_narrative_cannot_cite_a_missing_or_unselected_source(engine_workspace, monkeypatch, reference):
    w = engine_workspace
    state, output = extracted(w)
    chunks = [{'reviewId': f'E{i+1}', 'document_id': w.plan['id'], 'document_name': 'Synthetic.txt', 'content': 'Synthetic evidence'} for i in range(2)]
    force_answer(w, monkeypatch, {'status': '已满足', 'analysis': f'根据{reference}可确认符合', 'evidence': [
        {'source_id': 'E1', 'kind': '支持', 'analysis': '提供方案参数'}]})
    assert w.engine.judge(state['report'], output['requirements'][0], chunks)['status'] == '待人工复核'


@pytest.mark.parametrize('summary', ['方案10分钟，不低于8小时', '方案10小时，不高于8小时', '方案10小时，小于8小时'])
def test_polishing_cannot_change_units_or_comparison_direction(engine_workspace, monkeypatch, summary):
    w = engine_workspace
    report = prepared(w)['report']
    rows = [{'id': 'R001', 'question': '续航不得低于8小时', 'answer': {'status': '已满足', 'analysis': '方案10小时，不低于8小时，来源E1', 'recommendation': '复核'}}]
    force_answer(w, monkeypatch, {'sections': [{'id': 'R001', 'status': '已满足', 'headline': '续航条件', 'summary': summary, 'action': '复核'}]})
    with pytest.raises(ReviewOutputError):
        w.engine.polish_batch(report, rows)


def test_template_file_does_not_prove_requested_report_exists(engine_workspace, monkeypatch):
    w = engine_workspace
    state, output = extracted(w)
    w.knowledge.remove_documents([w.report_file['id']], w.dataset['id'])
    w.add('设备测试报告模板.txt', 'Only an empty template.')
    force_answer(w, monkeypatch, {'matches': []})
    result = w.engine.check_document(state['report'], output['requirements'][1])
    assert result['status'] == '证据不足' and not result['evidence']
    assert '模板' in result['analysis']


@pytest.mark.parametrize('result', [
    {'results': [{'index': 1, 'relevance_score': .8}, {'index': 0, 'relevance_score': .2}]},
    {'results': []}, {'results': [{'index': 0, 'relevance_score': 'NaN'}]},
    {'results': [{'index': 0, 'relevance_score': .8}, {'index': 0, 'relevance_score': .2}]},
    {'results': [None]}, {'results': [{'index': True, 'relevance_score': .8}]}])
def test_rerank_connection_test_really_checks_protocol(workspace, monkeypatch, result):
    app, client = workspace
    model = app.state.models.save({'name': 'audit-rerank', 'model_type': 'rerank', 'base_url': 'https://synthetic.invalid/v1'})
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=result)
    original = httpx.Client
    monkeypatch.setattr('backend.retrieval.httpx.Client', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    response = client.post('/settings/models/' + model['id'] + '/test')
    assert len(requests) == 1 and requests[0].url.path == '/v1/rerank'
    assert response.status_code == (200 if result['results'] and len(result['results']) == 2 and result['results'][0]['index'] == 1 else 502)
    payload = json.loads(requests[0].content)
    assert len(payload['documents']) == 2 and all(text.startswith('合成测试文字：') for text in payload['documents'])
