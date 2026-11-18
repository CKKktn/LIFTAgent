"""Job lifecycle tests with real durable graph checkpoints and synthetic data."""
from __future__ import annotations

import copy
import asyncio
import json
import threading
import time
from typing import TypedDict

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from starlette.middleware.sessions import SessionMiddleware

from backend.config import Settings
from backend.db import Database, dumps, now, uid
from backend.review_jobs import ReviewJobService, make_review_jobs_router


class Knowledge:
    def dataset(self, identifier):
        if identifier != "kb-test":
            raise HTTPException(404, "知识库不存在")
        return {"id": identifier, "name": "合成资料库"}

    def document(self, identifier, dataset_id):
        if dataset_id != "kb-test" or identifier not in {"contract", "plan"}:
            raise HTTPException(404, "文档不存在或不属于该知识库")
        return {"id": identifier, "dataset_id": dataset_id, "name": identifier + ".txt"}


class Models:
    def find(self, identifier, kind):
        if identifier not in {"", "fake-model"} or kind != "chat":
            raise HTTPException(404, "模型不存在")
        return {"id": "fake-model", "name": "合成模型"}


class Records:
    def __init__(self, db):
        self.db = db

    def get(self, identifier):
        row = self.db.one("SELECT * FROM review_records WHERE id=?", (identifier,))
        if not row:
            raise HTTPException(404, "记录不存在")
        return {**row, "report": json.loads(row["report"])}

    def save(self, body, identifier=None, *, _workflow=False):
        assert _workflow, "Only workflow-owned writes are expected in these fixtures"
        report = body["report"]
        with self.db.transaction() as connection:
            if identifier:
                row = connection.execute("SELECT revision FROM review_records WHERE id=?", (identifier,)).fetchone()
                if not row or row["revision"] != body.get("revision"):
                    raise HTTPException(409, "记录已修改")
                connection.execute("UPDATE review_records SET report=?,status=?,revision=revision+1,updated_at=? WHERE id=?", (dumps(report), body["status"], now(), identifier))
            else:
                identifier = uid()
                connection.execute("INSERT INTO review_records(id,report,title,status,kb,kb_name,contracts,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (identifier, dumps(report), report["title"], body["status"], report["kb"], "合成资料库", dumps(report["baselineDocs"]), now(), now()))
        return self.get(identifier)


class ReviewStopped(Exception):
    pass


class ReviewStale(Exception):
    pass


class Engine:
    def __init__(self):
        self.calls = 0
        self.fail_once = False
        self.stale = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def assert_materials(self, report, material_signature, model_signature):
        if self.stale:
            raise ReviewStale("synthetic material version changed")
        assert material_signature == "synthetic-material-v1"
        assert model_signature == "synthetic-model-v1"

    def confirm_requirements(self, report, submitted):
        if any(not item.get("title") or not item.get("criterion") for item in submitted):
            raise ValueError("bad synthetic input")
        return copy.deepcopy(submitted)


class State(TypedDict, total=False):
    task_id: str
    request: dict
    report: dict
    stage: int
    phase: str
    message: str
    material_signature: str
    model_signature: str


def graph_factory(engine, saver, controls):
    def guard(state):
        if controls.should_stop(state["task_id"]):
            raise ReviewStopped()

    def prepare(state):
        guard(state)
        report = {**state["report"], "requirements": [{"id": "R001", "type": "numeric", "title": "合成续航", "criterion": "至少8小时"}], "awaitingConfirmation": True}
        return {"report": report, "material_signature": "synthetic-material-v1", "model_signature": "synthetic-model-v1", "stage": 1, "phase": "extract", "message": "合成要求已提取"}

    def confirm(state):
        guard(state)
        answer = interrupt({"type": "confirm_requirements", "requirements": state["report"]["requirements"]})
        report = {**state["report"], "requirements": answer["requirements"], "confirmed": True, "awaitingConfirmation": False}
        return {"report": report, "stage": 2, "phase": "check"}

    def check(state):
        guard(state)
        engine.assert_materials(state["report"], state["material_signature"], state["model_signature"])
        engine.entered.set()
        assert engine.release.wait(timeout=10)
        engine.calls += 1
        retryable = engine.fail_once and engine.calls == 1
        result = {"requirementId": "R001", "status": "待人工复核" if retryable else "已满足", "retryable": retryable,
                  "analysis": "仅合成测试，不调用模型", "evidence": []}
        return {"report": {**state["report"], "results": [result], "complete": False}, "stage": 2, "message": "已保存合成条目"}

    def finish(state):
        guard(state)
        return {"report": {**state["report"], "complete": True}, "stage": 3, "phase": "report", "message": "合成报告已保存"}

    def retry(state):
        guard(state)
        decision = interrupt({"type": "retry_optional"})
        assert decision.get("retry") is True
        return {"report": {**state["report"], "complete": False}}

    builder = StateGraph(State)
    for name, node in [("prepare", prepare), ("confirm", confirm), ("check", check), ("finish", finish), ("await_retry", retry)]:
        builder.add_node(name, node)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "confirm")
    builder.add_edge("confirm", "check")
    builder.add_edge("check", "finish")
    builder.add_conditional_edges("finish", lambda state: "await_retry" if any(item.get("retryable") for item in state["report"]["results"]) else END)
    builder.add_edge("await_retry", "check")
    return builder.compile(checkpointer=saver)


def wait_status(service, identifier, status, owner="owner"):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = service.get(identifier, owner)
        if result["status"] == status:
            return result
        if result["status"] in {"failed", "stale"} and status not in {"failed", "stale"}:
            pytest.fail("Unexpected task state: " + repr(result))
        time.sleep(.01)
    pytest.fail("Timed out waiting for " + status)


@pytest.fixture
def workspace(tmp_path):
    db = Database(tmp_path / "workspace.sqlite3")
    records, engine = Records(db), Engine()
    service = ReviewJobService(db, Models(), Knowledge(), None, records, Settings(data_dir=tmp_path), engine=engine, graph_factory=graph_factory)
    yield db, records, engine, service
    engine.release.set()
    service.close()
    deadline = time.monotonic() + 5
    while service._futures and time.monotonic() < deadline:
        time.sleep(.01)


def create(service):
    return service.create("owner", {"kb": "kb-test", "baseline_document_ids": ["contract"], "model_id": "fake-model", "scope": "dataset", "title": "合成任务"})


def confirm(service, job):
    return service.resume(job["id"], "owner", {"revision": job["revision"], "requirements": job["report"]["requirements"]})


def test_waits_for_explicit_confirmation_and_persists_report(workspace):
    db, records, engine, service = workspace
    job = wait_status(service, create(service)["id"], "waiting_confirmation")
    assert engine.calls == 0
    assert job["report"]["awaitingConfirmation"]
    confirm(service, job)
    final = wait_status(service, job["id"], "completed")
    assert final["report"]["complete"] and engine.calls == 1
    assert len(final["report"]["results"]) == 1
    assert records.get(final["record_id"])["report"]["results"] == final["report"]["results"]
    assert db.one("SELECT COUNT(*) AS n FROM review_job_events")["n"] >= 6


def test_owner_and_command_revision_are_required(workspace):
    _, _, engine, service = workspace
    job = wait_status(service, create(service)["id"], "waiting_confirmation")
    with pytest.raises(HTTPException) as wrong_owner:
        service.get(job["id"], "stranger")
    assert wrong_owner.value.status_code == 404
    assert service.list("stranger") == []
    with pytest.raises(HTTPException) as stale:
        service.resume(job["id"], "owner", {"revision": job["revision"] - 1, "requirements": job["report"]["requirements"]})
    assert stale.value.status_code == 409
    with pytest.raises(HTTPException) as invalid:
        service.resume(job["id"], "owner", {"revision": job["revision"], "requirements": [{"title": ""}]})
    assert invalid.value.status_code == 400
    assert engine.calls == 0


def test_stop_accepts_progress_revision_but_confirmation_stays_strict(workspace):
    _, _, engine, service = workspace
    job = wait_status(service, create(service)["id"], "waiting_confirmation")
    assert job['revision'] > 1
    stopped = service.stop(job['id'], 'owner', {'revision': 1})
    assert stopped['status'] == 'stopped'
    with pytest.raises(HTTPException) as invalid:
        service.resume(job['id'], 'owner', {'revision': 1})
    assert invalid.value.status_code == 409 and not engine.calls


def test_stop_during_final_checkpoint_projection_is_not_overwritten(workspace, monkeypatch):
    _, _, _, service = workspace
    original = service._project
    def stop_after_projection(identifier, node, update):
        original(identifier, node, update)
        if node == 'checkpoint_projection':
            current = service.get(identifier, 'owner')
            if current['status'] in {'running','pending'}:
                service.stop(identifier, 'owner', {'revision': current['revision']})
    monkeypatch.setattr(service, '_project', stop_after_projection)
    job = create(service)
    stopped = wait_status(service, job['id'], 'stopped')
    assert stopped['status'] == 'stopped'


def test_stop_preserves_completed_node_and_continue_does_not_repeat_it(workspace):
    _, _, engine, service = workspace
    engine.release.clear()
    job = wait_status(service, create(service)["id"], "waiting_confirmation")
    confirm(service, job)
    assert engine.entered.wait(timeout=5)
    current = service.get(job["id"], "owner")
    stopping = service.stop(job["id"], "owner", {"revision": current["revision"]})
    assert stopping["status"] == "stopping"
    engine.release.set()
    stopped = wait_status(service, job["id"], "stopped")
    assert engine.calls == 1 and len(stopped["report"]["results"]) == 1
    service.resume(job["id"], "owner", {"revision": stopped["revision"]})
    final = wait_status(service, job["id"], "completed")
    assert engine.calls == 1 and len(final["report"]["results"]) == 1


def test_restart_reloads_interrupt_checkpoint_without_model_calls(workspace):
    db, records, engine, service = workspace
    job = wait_status(service, create(service)["id"], "waiting_confirmation")
    service.close()
    deadline = time.monotonic() + 5
    while service._futures and time.monotonic() < deadline:
        time.sleep(.01)
    restored = ReviewJobService(db, Models(), Knowledge(), None, records, service.settings, engine=engine, graph_factory=graph_factory)
    try:
        existing = restored.get(job["id"], "owner")
        assert engine.calls == 0 and existing["status"] == "waiting_confirmation"
        confirm(restored, existing)
        final = wait_status(restored, job["id"], "completed")
        assert engine.calls == 1 and final["report"]["confirmed"]
    finally:
        restored.close()


def test_retry_uses_optional_interrupt_and_changes_only_failed_result(workspace):
    _, _, engine, service = workspace
    engine.fail_once = True
    job = wait_status(service, create(service)["id"], "waiting_confirmation")
    confirm(service, job)
    final = wait_status(service, job["id"], "completed")
    assert final["can_retry"] and engine.calls == 1
    service.retry(job["id"], "owner", {"revision": final["revision"]})
    retried = wait_status(service, job["id"], "completed")
    assert not retried["can_retry"] and engine.calls == 2
    assert len(retried["report"]["results"]) == 1


def test_manual_report_edit_is_never_overwritten_by_old_checkpoint(workspace):
    db, _, _, service = workspace
    job = wait_status(service, create(service)["id"], "waiting_confirmation")
    confirm(service, job)
    final = wait_status(service, job["id"], "completed")
    edited = {**final["report"], "title": "人工修订的归档标题"}
    db.execute("UPDATE review_records SET report=?,revision=revision+1 WHERE id=?", (dumps(edited), final["record_id"]))
    current = service.get(job["id"], "owner")
    assert current["record_modified"] and current["report"]["title"] == "人工修订的归档标题"
    assert not current["can_retry"] and not current["can_resume"]


def test_changed_materials_refuse_confirmation_and_execution(workspace):
    _, _, engine, service = workspace
    job = wait_status(service, create(service)["id"], "waiting_confirmation")
    engine.stale = True
    with pytest.raises(HTTPException) as changed:
        confirm(service, job)
    assert changed.value.status_code == 409
    assert service.get(job["id"], "owner")["status"] == "stale" and engine.calls == 0


def test_event_cursor_replay_is_durable_and_graph_is_actual_compiled_graph(workspace):
    db, _, _, service = workspace
    job = wait_status(service, create(service)["id"], "waiting_confirmation")
    events = service.events(job["id"], "owner")
    assert events and [event["id"] for event in events] == sorted(event["id"] for event in events)
    assert service.events(job["id"], "owner", events[-1]["id"]) == []
    assert "report" not in events[0] and "api_key" not in dumps(events)
    graph = service.graph_description(job["id"], "owner")
    assert {"prepare", "confirm", "check", "finish", "await_retry"}.issubset({node["id"] for node in graph["nodes"]})
    assert "confirm" in graph["mermaid"] and graph["trace"]
    assert (service.settings.data_dir / "review-checkpoints.sqlite3").is_file()
    with sqlite_connect(service.settings.data_dir / "review-checkpoints.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] > 0


def sqlite_connect(path):
    import sqlite3
    return sqlite3.connect(path)


def test_router_authentication_and_static_graph_route(workspace):
    db, _, _, service = workspace
    db.execute("INSERT INTO users(id,username,password_hash,created_at) VALUES (?,?,?,?)", ("owner", "synthetic", "unused", now()))
    app = FastAPI()
    app.state.db = db
    app.add_middleware(SessionMiddleware, secret_key="synthetic-only-secret")
    @app.get("/test-login")
    def login(request):
        request.session["user_id"] = "owner"
        return {"ok": True}
    # Give FastAPI the actual Request annotation instead of exposing a query argument.
    from fastapi import Request
    login.__annotations__["request"] = Request
    app.include_router(make_review_jobs_router(service))
    with TestClient(app) as client:
        assert client.get("/api/v1/review-jobs").status_code == 401
        # A signed fixture session is created without any daily account access.
        from itsdangerous import TimestampSigner
        from base64 import b64encode
        cookie = TimestampSigner("synthetic-only-secret").sign(b64encode(dumps({"user_id": "owner"}).encode())).decode()
        client.cookies.set("session", cookie)
        response = client.get("/api/v1/review-jobs/graph")
        assert response.status_code == 200 and "mermaid" in response.json()
        assert client.get("/api/v1/review-jobs/missing").status_code == 404
        assert client.post("/api/v1/review-jobs", json={"kb": "bad"}).status_code == 404


def test_disconnect_only_closes_observation_and_reconnect_uses_persistent_event_cursor(workspace):
    _, _, engine, service = workspace
    job = wait_status(service, create(service)["id"], "waiting_confirmation")
    saved_events = service.events(job["id"], "owner")
    after = saved_events[0]["id"]
    endpoint = next(route.endpoint for route in make_review_jobs_router(service).routes if route.path.endswith('/{identifier}/events'))
    class Observer:
        headers = {"last-event-id": str(after)}
        async def is_disconnected(self):
            return False
    async def observe_once():
        response = await endpoint(job["id"], Observer(), after=0, owner="owner")
        first = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return first
    event = asyncio.run(observe_once())
    assert "event: state" in event and int(event.splitlines()[0].split(': ')[1]) > after
    assert service.get(job["id"], "owner")["status"] == "waiting_confirmation" and engine.calls == 0
    confirm(service, service.get(job["id"], "owner"))
    assert wait_status(service, job["id"], "completed")["report"]["complete"]


def test_restart_marks_interrupted_runner_stopped_without_executing_model(workspace):
    db, records, engine, service = workspace
    job = wait_status(service, create(service)["id"], "waiting_confirmation")
    service.close()
    deadline = time.monotonic() + 5
    while service._futures and time.monotonic() < deadline:
        time.sleep(.01)
    # Simulate a process that exited while its durable metadata said running.
    db.execute("UPDATE review_jobs SET status='running' WHERE id=?", (job["id"],))
    restored = ReviewJobService(db, Models(), Knowledge(), None, records, service.settings, engine=engine, graph_factory=graph_factory)
    try:
        stopped = restored.get(job["id"], "owner")
        assert stopped["status"] == "stopped" and stopped["can_resume"] and engine.calls == 0
        restored.resume(job["id"], "owner", {"revision": stopped["revision"]})
        paused = wait_status(restored, job["id"], "waiting_confirmation")
        assert paused["report"]["requirements"] and engine.calls == 0
    finally:
        restored.close()


@pytest.fixture
def real_workflow(tmp_path):
    from backend.records import RecordService
    from backend.review_graph import build_review_graph
    from test_review_graph import Engine as BusinessFixture

    class WorkflowEngine(BusinessFixture):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()
            self.release.set()
        def prepare(self, payload):
            value = super().prepare(payload)
            value['report'].update(kb=payload['kb'], kbName='合成资料库', title=payload['title'],
                                   baselineDocs=[{'id':'contract','name':'contract.txt'}])
            return value
        def judge(self, report, requirement, chunks):
            self.entered.set()
            assert self.release.wait(timeout=10)
            return super().judge(report,requirement,chunks)

    db = Database(tmp_path / 'workspace.sqlite3')
    records = RecordService(db, Knowledge(), None)
    engine = WorkflowEngine()
    service = ReviewJobService(db, Models(), Knowledge(), None, records, Settings(data_dir=tmp_path), engine=engine, graph_factory=build_review_graph)
    yield db, records, engine, service
    engine.release.set()
    service.close()
    deadline=time.monotonic()+5
    while service._futures and time.monotonic()<deadline:
        time.sleep(.01)


def test_actual_three_subgraphs_publish_progress_and_report_through_real_record_service(real_workflow):
    _, records, engine, service = real_workflow
    job = wait_status(service,create(service)['id'],'waiting_confirmation')
    assert job['requirements_count']==3 and not any(call[0]=='judge' for call in engine.calls)
    confirm(service,job)
    final = wait_status(service,job['id'],'completed')
    assert final['results_count']==3 and final['stage']==3 and final['report']['complete']
    assert records.get(final['record_id'])['report']['results']==final['report']['results']
    names={event['node'] for event in service.events(job['id'],'owner')}
    assert any('extract_requirements/' in name for name in names)
    assert any('check_requirements/numeric_retrieve' in name for name in names)
    assert any('check_requirements/functional_retrieve' in name for name in names)
    assert any('check_requirements/document_check' in name for name in names)
    description=service.graph_description(job['id'],'owner')
    assert len(description['nodes'])>20 and description['trace']


def test_actual_subgraph_stop_resumes_without_rejudging_saved_item(real_workflow):
    _, _, engine, service = real_workflow
    engine.kinds=('numeric',)
    engine.release.clear()
    job=wait_status(service,create(service)['id'],'waiting_confirmation')
    confirm(service,job)
    assert engine.entered.wait(timeout=5)
    current=service.get(job['id'],'owner')
    service.stop(job['id'],'owner',{'revision':current['revision']})
    engine.release.set()
    stopped=wait_status(service,job['id'],'stopped')
    assert len([call for call in engine.calls if call==('judge','R001')])==1
    service.resume(job['id'],'owner',{'revision':stopped['revision']})
    final=wait_status(service,job['id'],'completed')
    assert final['results_count']==1
    assert len([call for call in engine.calls if call==('judge','R001')])==1


def test_actual_retry_resumes_root_interrupt_and_keeps_successful_branch(real_workflow):
    _, _, engine, service=real_workflow
    engine.kinds=('numeric','functional');engine.fail_item='R002'
    job=wait_status(service,create(service)['id'],'waiting_confirmation');confirm(service,job)
    final=wait_status(service,job['id'],'completed')
    assert final['can_retry'] and final['results_count']==2
    engine.fail_item=None
    service.retry(job['id'],'owner',{'revision':final['revision']})
    retried=wait_status(service,job['id'],'completed')
    assert not retried['can_retry'] and len([call for call in engine.calls if call==('judge','R001')])==1
    assert len([call for call in engine.calls if call==('judge','R002')])==3


def test_report_only_failure_exposes_retry_and_retries_polish_without_rejudging(real_workflow):
    from backend.review_engine import ReviewOutputError
    _, _, engine, service=real_workflow
    original=engine.polish_batch
    attempts=[]
    def polish(report,rows):
        attempts.append(len(rows))
        if len(attempts)<=2:
            raise ReviewOutputError('合成整理失败')
        return original(report,rows)
    engine.polish_batch=polish
    job=wait_status(service,create(service)['id'],'waiting_confirmation');confirm(service,job)
    first=wait_status(service,job['id'],'completed')
    assert first['report']['complete'] and first['report']['workflowCanRetry'] is True
    assert not any(item.get('retryable') for item in first['report']['results'])
    assert first['can_retry'] and len(attempts)==2
    successful_calls=[call for call in engine.calls if call[0] in ('judge','document')]
    service.retry(job['id'],'owner',{'revision':first['revision']})
    retried=wait_status(service,job['id'],'completed')
    assert retried['report']['complete'] and retried['report']['workflowCanRetry'] is False
    assert not retried['can_retry'] and len(attempts)==3
    assert [call for call in engine.calls if call[0] in ('judge','document')]==successful_calls
    assert len(retried['report']['results'])==3


def test_pending_job_without_first_checkpoint_restarts_only_after_explicit_resume(real_workflow):
    from backend.review_graph import build_review_graph
    db, records, engine, service=real_workflow
    # Persist the accepted job while simulating a process exit before its queued
    # worker is dispatched. There must not be an invented graph checkpoint.
    service._submit=lambda identifier,graph_input:None
    pending=create(service)
    assert pending['status']=='pending' and not engine.calls
    assert service.checkpointer.get_tuple(service._config(pending['id'])) is None
    service.close()
    restored=ReviewJobService(db,Models(),Knowledge(),None,records,service.settings,
                             engine=engine,graph_factory=build_review_graph)
    try:
        stopped=restored.get(pending['id'],'owner')
        assert stopped['status']=='stopped' and stopped['can_resume'] and not engine.calls
        restored.resume(pending['id'],'owner',{'revision':stopped['revision']})
        paused=wait_status(restored,pending['id'],'waiting_confirmation')
        assert paused['requirements_count']==3 and paused['report']['awaitingConfirmation']
        assert engine.calls==[('extract','batch')]
        assert restored.graph.get_state(restored._config(pending['id'])).values['task_id']==pending['id']
    finally:
        restored.close()
        deadline=time.monotonic()+5
        while restored._futures and time.monotonic()<deadline:
            time.sleep(.01)


def test_stopped_after_retrieval_rejects_changed_materials_before_any_judge_call(real_workflow):
    _, _, engine, service=real_workflow
    engine.kinds=('numeric',)
    original=engine.retrieve
    job=wait_status(service,create(service)['id'],'waiting_confirmation')
    def stop_after_retrieval(*args,**kwargs):
        chunks=original(*args,**kwargs)
        current=service.get(job['id'],'owner')
        service.stop(job['id'],'owner',{'revision':current['revision']})
        return chunks
    engine.retrieve=stop_after_retrieval
    confirm(service,job)
    stopped=wait_status(service,job['id'],'stopped')
    assert any(call[0]=='retrieve' for call in engine.calls) and not any(call[0]=='judge' for call in engine.calls)
    engine.version='changed-after-stop'
    with pytest.raises(HTTPException) as stale:
        service.resume(job['id'],'owner',{'revision':stopped['revision']})
    assert stale.value.status_code==409
    assert service.get(job['id'],'owner')['status']=='stale'
    assert not any(call[0]=='judge' for call in engine.calls)


def test_retry_rejects_changed_materials_before_any_repeated_model_work(real_workflow):
    _, _, engine, service=real_workflow
    engine.kinds=('numeric','functional');engine.fail_item='R002'
    job=wait_status(service,create(service)['id'],'waiting_confirmation');confirm(service,job)
    final=wait_status(service,job['id'],'completed')
    assert final['can_retry']
    before=list(engine.calls)
    engine.version='changed-before-retry'
    with pytest.raises(HTTPException) as stale:
        service.retry(job['id'],'owner',{'revision':final['revision']})
    assert stale.value.status_code==409 and engine.calls==before
    assert service.get(job['id'],'owner')['status']=='stale'


def test_actual_no_valid_requirements_ends_failed_without_entering_checking(real_workflow):
    _, _, engine, service=real_workflow
    engine.no_requirements=True
    failed=wait_status(service,create(service)['id'],'failed')
    assert failed['requirements_count']==0 and not failed['report']['complete']
    assert not any(call[0] in ('judge','retrieve','document') for call in engine.calls)
    assert failed['phase']=='failed' and '未取得可验证技术要求' in failed['message']
    assert not failed['can_resume']
    with pytest.raises(HTTPException) as terminal:
        service.resume(failed['id'],'owner',{'revision':failed['revision']})
    assert terminal.value.status_code==409
    assert service.list('owner',active=True)==[]


def test_terminal_extraction_failures_release_creation_quota(real_workflow):
    db, _, engine, service=real_workflow
    engine.no_requirements=True
    for _ in range(8):
        failed=wait_status(service,create(service)['id'],'failed')
        assert not failed['can_resume']
    assert service.list('owner',active=True)==[]
    ninth=wait_status(service,create(service)['id'],'failed')
    assert ninth['requirements_count']==0
    assert db.one('SELECT COUNT(*) AS count FROM review_jobs')['count']==9


def test_failed_record_cleanup_preserves_other_live_task_and_its_checkpoint(real_workflow):
    db, records, engine, service=real_workflow
    engine.no_requirements=True
    failed=wait_status(service,create(service)['id'],'failed')
    engine.no_requirements=False
    waiting=wait_status(service,create(service)['id'],'waiting_confirmation')
    records.delete(failed['record_id'],failed['record_revision'])
    service.forget_completed_record(failed['record_id'])
    assert not db.one('SELECT id FROM review_jobs WHERE id=?',(failed['id'],))
    assert not db.one('SELECT id FROM review_job_events WHERE job_id=?',(failed['id'],))
    assert service.checkpointer.get_tuple(service._config(failed['id'])) is None
    assert service.get(waiting['id'],'owner')['status']=='waiting_confirmation'
    assert service.events(waiting['id'],'owner')
    assert service.checkpointer.get_tuple(service._config(waiting['id'])) is not None


def test_completed_report_delete_removes_only_associated_job_events_and_checkpoints(real_workflow):
    db, records, _, service=real_workflow
    job=wait_status(service,create(service)['id'],'waiting_confirmation');confirm(service,job)
    final=wait_status(service,job['id'],'completed')
    with pytest.raises(HTTPException):
        service.forget_completed_record(final['record_id'])
    records.delete(final['record_id'],final['record_revision'])
    service.forget_completed_record(final['record_id'])
    assert not db.one('SELECT id FROM review_jobs WHERE id=?',(job['id'],))
    assert not db.one('SELECT id FROM review_job_events WHERE job_id=?',(job['id'],))
    assert service.checkpointer.get_tuple(service._config(job['id'])) is None


def test_task_request_never_persists_unexpected_secret_fields_and_baseline_limit_is_checked_before_creation(workspace):
    db, _, _, service = workspace
    payload={'kb':'kb-test','baseline_document_ids':['contract'],'title':'合成任务',
             'api_key':'SHOULD_NEVER_ENTER_WORKFLOW','base_url':'https://secret.invalid/?key=SHOULD_NEVER_ENTER_WORKFLOW'}
    job=wait_status(service,service.create('owner',payload)['id'],'waiting_confirmation')
    stored=db.one('SELECT request,report FROM review_jobs WHERE id=?',(job['id'],))
    assert 'SHOULD_NEVER_ENTER_WORKFLOW' not in dumps(stored)
    assert 'SHOULD_NEVER_ENTER_WORKFLOW' not in dumps(service.events(job['id'],'owner'))
    assert 'SHOULD_NEVER_ENTER_WORKFLOW' not in dumps(service.graph.get_state(service._config(job['id'])).values)
    count=db.one('SELECT COUNT(*) AS count FROM review_jobs')['count']
    with pytest.raises(HTTPException) as oversized:
        service.create('owner',{'kb':'kb-test','baseline_document_ids':['contract']*21,'title':'过多依据'})
    assert oversized.value.status_code==400
    assert db.one('SELECT COUNT(*) AS count FROM review_jobs')['count']==count


def test_unknown_worker_exception_never_exposes_response_bodies_or_keys(workspace):
    db, _, _, service=workspace
    def broken(*args,**kwargs):
        raise RuntimeError('Authorization: Bearer SECRET_EXCEPTION_SENTINEL')
        yield
    service.graph.stream=broken
    failed=wait_status(service,create(service)['id'],'failed')
    assert 'SECRET_EXCEPTION_SENTINEL' not in dumps(failed)
    assert 'SECRET_EXCEPTION_SENTINEL' not in dumps(service.events(failed['id'],'owner'))
    assert 'SECRET_EXCEPTION_SENTINEL' not in db.one('SELECT report FROM review_records WHERE id=?',(failed['record_id'],))['report']


def test_old_workflow_versions_are_readable_but_never_resumed_or_drawn_as_current_graph(workspace):
    db, _, _, service=workspace
    job=wait_status(service,create(service)['id'],'waiting_confirmation')
    db.execute('UPDATE review_jobs SET workflow_version=? WHERE id=?',('older-incompatible-v0',job['id']))
    old=service.get(job['id'],'owner')
    assert old['report']['requirements'] and not old['workflow_compatible']
    assert not old['can_resume'] and not old['can_retry']
    with pytest.raises(HTTPException) as incompatible:
        confirm(service,old)
    assert incompatible.value.status_code==409
    with pytest.raises(HTTPException) as diagram:
        service.graph_description(job['id'],'owner')
    assert diagram.value.status_code==409


@pytest.mark.parametrize('reason,shown',[
    ('模型服务返回 HTTP 401，请检查模型名称、地址、密钥和额度。',True),
    ('模型连接失败：https://secret.invalid/?key=SECRET_SENTINEL',False),
    ('模型错误 Authorization: Bearer SECRET_SENTINEL',False),
])
def test_only_short_known_sanitized_fatal_reason_can_reach_job_message(workspace,reason,shown):
    from backend.review_engine import ReviewFatalError
    _, _, _, service=workspace
    def broken(*args,**kwargs):
        raise ReviewFatalError(reason)
        yield
    service.graph.stream=broken
    failed=wait_status(service,create(service)['id'],'failed')
    assert (failed['message']==reason)==shown
    assert 'SECRET_SENTINEL' not in dumps(failed)
