"""Final independent-audit regressions for takeover, audit reads and startup safety."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import webbrowser

import pytest

from backend.collab_jobs import CollabJobService
from backend import main
from test_collab_workflow import workspace, collab_workspace, create, waiting, act, wait_job, wait_service, settled


def test_old_service_shutdown_cannot_pause_a_new_worker(collab_workspace):
    w = collab_workspace
    w.control['hold'] = 'COLLAB_EVIDENCE_V1'
    w.control['release'].clear()
    first = create(w)
    assert w.control['entered'].wait(6)
    old = w.app.state.collab_jobs
    restored = CollabJobService(w.app.state.db, w.models, w.app.state.knowledge, w.app.state.retrieval, w.app.state.settings)
    try:
        stopped = restored.get(first['id'], w.owner)
        w.control['entered'].clear()
        restored.resume(first['id'], w.owner, {'revision': stopped['revision']})
        assert w.control['entered'].wait(6)
        assert restored.get(first['id'], w.owner)['status'] == 'running'
        old.close()
        assert restored.get(first['id'], w.owner)['status'] == 'running'
    finally:
        w.control['release'].set()
        settled(old)
        settled(restored)
        restored.close()


def test_review_audit_endpoint_preserves_original_and_revised_opinions(collab_workspace):
    w = collab_workspace
    job = waiting(w)
    assert act(w, job, 'review', decision='revise', answer='人工修订：需补充资料后复核。', notes='核对范围及原件后记录').status_code == 200
    assert wait_job(w, job['id'])['status'] == 'completed'
    path = '/api/v1/collab-jobs/' + job['id'] + '/decisions'
    response = w.client.get(path)
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]['draft'] == job['answer']
    assert rows[0]['final_text'] == '人工修订：需补充资料后复核。'
    assert rows[0]['notes'] == '核对范围及原件后记录' and rows[0]['actor_id'] == w.owner
    w.client.post('/auth/logout')
    assert w.client.get(path).status_code == 401


def test_duplicate_launch_is_rejected_before_application_data_initialization(tmp_path, monkeypatch):
    unrelated = tmp_path / 'another-copy'
    (unrelated / 'assets').mkdir(parents=True)
    _check_occupied_port(tmp_path, monkeypatch, unrelated, reuse=False)


def test_duplicate_launch_opens_the_running_working_copy(tmp_path, monkeypatch):
    _check_occupied_port(tmp_path, monkeypatch, tmp_path / 'frontend', reuse=True)


def _check_occupied_port(tmp_path, monkeypatch, served_frontend, *, reuse):
    assets = tmp_path / 'frontend' / 'assets'
    assets.mkdir(parents=True)
    opened = []

    def open_browser(url):
        opened.append(url)
        return True

    monkeypatch.setattr(main, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(webbrowser, 'open', open_browser)
    handler = partial(SimpleHTTPRequestHandler, directory=str(served_frontend))
    with ThreadingHTTPServer(('127.0.0.1', 0), handler) as server:
        port = server.server_port
        monkeypatch.setenv('APP_HOST', '127.0.0.1')
        monkeypatch.setenv('APP_PORT', str(port))
        monkeypatch.setenv('APP_DATA_DIR', str(tmp_path / 'empty-workspace'))
        worker = Thread(target=server.serve_forever)
        worker.start()
        try:
            if reuse:
                assert main.serve() == 0
                assert opened == [f'http://127.0.0.1:{port}/']
            else:
                with pytest.raises(RuntimeError, match='already in use'):
                    main.serve()
                assert opened == []
        finally:
            server.shutdown()
            worker.join()
    assert not list(assets.iterdir())
    assert not (tmp_path / 'empty-workspace').exists()
