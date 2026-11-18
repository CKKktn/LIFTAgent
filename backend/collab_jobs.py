"""Durable collaborative-review jobs. Browser connections observe, never own, execution."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import logging
import math
import sqlite3
import threading
import time

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .collab_images import decode_image, read_image
from .db import dumps, now, uid
from .models import provider_error
from .security import require_user

LOGGER = logging.getLogger(__name__)
RUNNING = {"pending", "running", "stopping"}
OPEN = RUNNING | {"waiting_confirmation", "stopped", "failed"}
TERMINAL = {"completed", "cancelled", "stale"}
PUBLIC_KEYS = {"standalone_question", "search_query", "intent", "retrieval_rounds", "auto_rounds", "manual_rounds",
               "references", "answer", "validation", "evidence_assessment", "answer_attempts", "needs_confirmation",
               "human_review", "final_answer", "final_status", "user_edited", "model_errors", "limitations"}


class CollabStale(Exception):
    pass


class CollabSuperseded(Exception):
    """An older service instance no longer owns writes for this job."""


def _digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


class CollabJobService:
    def __init__(self, db, models, knowledge, retrieval, settings, *, engine=None, graph_factory=None):
        from .collab_graph import CollabEngine, WORKFLOW_VERSION, build_collab_graph
        self.db, self.models, self.knowledge, self.settings = db, models, knowledge, settings
        self.engine = engine or CollabEngine(models, retrieval)
        self.workflow_version = WORKFLOW_VERSION
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="collaborative-review")
        self._futures, self._queued = {}, {}
        self._closing, self._checkpoint_closed = False, False
        self._instance_token, self._worker_local = uid(), threading.local()
        self._delta_clock = {}
        with db.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS collab_jobs (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    model_id TEXT NOT NULL, workflow_version TEXT NOT NULL,
                    status TEXT NOT NULL, phase TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1, request TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
                    user_message_id TEXT NOT NULL, assistant_message_id TEXT NOT NULL,
                    stop_requested INTEGER NOT NULL DEFAULT 0, last_event_id INTEGER NOT NULL DEFAULT 0,
                    request_id TEXT, worker_token TEXT NOT NULL DEFAULT '', pending_command TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS collab_jobs_session ON collab_jobs(session_id,created_at);
                CREATE INDEX IF NOT EXISTS collab_jobs_owner ON collab_jobs(owner_id,updated_at);
                CREATE UNIQUE INDEX IF NOT EXISTS collab_jobs_idempotency ON collab_jobs(owner_id,request_id) WHERE request_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS collab_job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES collab_jobs(id) ON DELETE CASCADE,
                    node TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS collab_events_job ON collab_job_events(job_id,id);
                CREATE TABLE IF NOT EXISTS collab_review_decisions (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES collab_jobs(id) ON DELETE CASCADE,
                    owner_id TEXT NOT NULL, decision TEXT NOT NULL, revision INTEGER NOT NULL,
                    draft TEXT NOT NULL, final_text TEXT NOT NULL, notes TEXT NOT NULL, query TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(collab_jobs)")}
            for name in ("worker_token", "pending_command"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE collab_jobs ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._checkpoint_connection = sqlite3.connect(str(settings.data_dir / "collab-checkpoints.sqlite3"), check_same_thread=False, timeout=30)
        self._checkpoint_connection.execute("PRAGMA journal_mode=WAL")
        self._checkpoint_connection.execute("PRAGMA busy_timeout=30000")
        self.checkpointer = SqliteSaver(self._checkpoint_connection, serde=JsonPlusSerializer(allowed_msgpack_modules=[]))
        self.checkpointer.setup()
        self.graph = (graph_factory or build_collab_graph)(self.engine, self.checkpointer, self)
        # Fence late writes from the previous instance even when it already
        # marked a job stopped or waiting before the new instance was created.
        # Ownership alone is not a user-visible revision/content change.
        with self.db.transaction() as connection:
            connection.execute("UPDATE collab_jobs SET worker_token=? WHERE status IN ('pending','running','stopping','waiting_confirmation','stopped','failed')", (self._instance_token,))
        # Startup recovers status only; resuming a paid call requires a user command.
        for row in db.rows("SELECT id FROM collab_jobs WHERE status IN ('pending','running','stopping')"):
            self._update(row["id"], status="stopped", stop_requested=0, worker_token=self._instance_token, node="service_restart",
                         message="服务已重启，协审进度已保留；请手动继续。")

    def _config(self, identifier):
        return {"configurable": {"thread_id": identifier}, "recursion_limit": 100}

    def _row(self, identifier, owner=None):
        row = self.db.one("SELECT * FROM collab_jobs WHERE id=?", (identifier,))
        if not row or owner is not None and row["owner_id"] != owner:
            raise HTTPException(404, "协审任务不存在")
        return row

    @staticmethod
    def _revision(row, revision):
        if type(revision) is not int or row["revision"] != revision:
            raise HTTPException(409, "协审内容或任务状态已变化，请刷新后重新核对；本次操作未提交")

    def _event(self, connection, identifier, node, kind, payload):
        cursor = connection.execute("INSERT INTO collab_job_events(job_id,node,kind,payload,created_at) VALUES (?,?,?,?,?)",
            (identifier, node, kind, dumps(payload), now()))
        connection.execute("UPDATE collab_jobs SET last_event_id=? WHERE id=?", (cursor.lastrowid, identifier))

    def _sync_message(self, connection, identifier):
        row = connection.execute("SELECT * FROM collab_jobs WHERE id=?", (identifier,)).fetchone()
        if not row:
            return
        session = connection.execute("SELECT messages FROM sessions WHERE id=?", (row["session_id"],)).fetchone()
        if not session:
            return
        messages = json.loads(session["messages"])
        index = next((index for index, message in enumerate(messages) if message.get("id") == row["assistant_message_id"]), None)
        if index is None:
            return
        result = json.loads(row["result"])
        state = {"completed": "complete", "cancelled": "cancelled", "failed": "error", "stale": "stale", "stopped": "stopped", "waiting_confirmation": "waiting_confirmation"}.get(row["status"], "running")
        answer = result.get("final_answer") if row["status"] == "completed" and isinstance(result.get("final_answer"), str) else result.get("answer", "")
        messages[index] = {**messages[index], "content": answer or "", "replyState": state,
            "reference": {"chunks": result.get("references", [])},
            "workflow": {"job_id": identifier, "status": row["status"], "phase": row["phase"],
                "message": row["message"], "validation": result.get("validation", {}),
                "review": result.get("human_review", {}), "retrieval_rounds": result.get("retrieval_rounds", 0)}}
        connection.execute("UPDATE sessions SET messages=?,revision=revision+1,updated_at=? WHERE id=?", (dumps(messages), now(), row["session_id"]))

    def _update(self, identifier, *, result_patch=None, status=None, phase=None, message=None, stop_requested=None, worker_token=None, pending_command=None, expected_worker_token=None, node="state", kind="state"):
        with self._lock, self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM collab_jobs WHERE id=?", (identifier,)).fetchone()
            if not row:
                return
            if expected_worker_token is not None and row["worker_token"] != expected_worker_token:
                return
            if getattr(self._worker_local, "job_id", None) == identifier and row["worker_token"] != self._instance_token:
                raise CollabSuperseded()
            result = json.loads(row["result"])
            if result_patch:
                result.update({key: copy.deepcopy(value) for key, value in result_patch.items() if key in PUBLIC_KEYS})
            values = {"result": dumps(result), "status": status if status is not None else row["status"],
                "phase": phase if phase is not None else row["phase"], "message": str(message)[:2000] if message is not None else row["message"],
                "stop_requested": stop_requested if stop_requested is not None else row["stop_requested"]}
            connection.execute("UPDATE collab_jobs SET result=?,status=?,phase=?,message=?,stop_requested=?,worker_token=?,pending_command=?,revision=revision+1,updated_at=? WHERE id=?",
                (values["result"], values["status"], values["phase"], values["message"], values["stop_requested"], worker_token if worker_token is not None else row["worker_token"], pending_command if pending_command is not None else row["pending_command"], now(), identifier))
            self._event(connection, identifier, node, kind, {"status": values["status"], "phase": values["phase"], "message": values["message"], "retrieval_rounds": result.get("retrieval_rounds", 0)})
            self._sync_message(connection, identifier)

    def _public(self, row):
        result, request = json.loads(row["result"]), json.loads(row["request"])
        trace = self.db.rows("SELECT node,payload,created_at FROM collab_job_events WHERE job_id=? AND kind<>'delta' ORDER BY id DESC LIMIT 80", (row["id"],))
        trace = [{"node": item["node"], **json.loads(item["payload"]), "time": item["created_at"], "created_at": item["created_at"]} for item in reversed(trace)]
        answer = result.get("final_answer") if row["status"] == "completed" and isinstance(result.get("final_answer"), str) else result.get("answer", "")
        return {key: row[key] for key in ("id", "session_id", "chat_id", "model_id", "status", "phase", "message", "revision", "user_message_id", "assistant_message_id", "last_event_id", "created_at", "updated_at")} | {
            "workflow_version": row["workflow_version"], "question": request["question"], "answer": answer or "",
            **({"image": request["image"]} if request.get("image") else {}),
            "reference": {"chunks": result.get("references", [])}, "standalone_question": result.get("standalone_question", request["question"]),
            "retrieval_rounds": result.get("retrieval_rounds", 0), "manual_rounds": result.get("manual_rounds", 0),
            "validation": result.get("validation", {}), "evidence_assessment": result.get("evidence_assessment", {}),
            "review": result.get("human_review", {}), "trace": trace,
            "can_resume": row["status"] in {"stopped", "failed"} and row["workflow_version"] == self.workflow_version,
            "requires_confirmation": row["status"] == "waiting_confirmation",
            "needs_confirmation": result.get("needs_confirmation", False), "limits": {"automatic_retrievals": 3, "human_supplements": 2},
        }

    def get(self, identifier, owner=None):
        return self._public(self._row(identifier, owner))

    def decisions(self, identifier, owner):
        self._row(identifier, owner)
        return self.db.rows("SELECT id,decision,revision,draft,final_text,notes,query,created_at,owner_id AS actor_id FROM collab_review_decisions WHERE job_id=? ORDER BY created_at,id", (identifier,))

    def list(self, owner, session_id="", chat_id=""):
        conditions, parameters = ["owner_id=?"], [owner]
        for column, value in (("session_id", session_id), ("chat_id", chat_id)):
            if value:
                conditions.append(column + "=?")
                parameters.append(value)
        rows = self.db.rows("SELECT * FROM collab_jobs WHERE " + " AND ".join(conditions) + " ORDER BY updated_at DESC LIMIT 200", parameters)
        return [self._public(row) for row in rows]

    def _context_signature(self, chat_id, dataset_ids, model_id):
        assistant = self.db.json_get("chats", chat_id)
        if not assistant:
            raise CollabStale()
        material = {"assistant": assistant, "datasets": [], "documents": [], "chunks": []}
        for identifier in dataset_ids:
            dataset = self.db.json_get("datasets", identifier)
            if not dataset:
                raise CollabStale()
            material["datasets"].append(dataset)
            material["documents"].extend(self.db.rows("SELECT id,name,run,enabled,generation,updated_at,chunk_count,metadata,size,path FROM documents WHERE dataset_id=? ORDER BY id", (identifier,)))
            material["chunks"].extend(self.db.rows("SELECT id,document_id,content,metadata,vector,embedding_id,ordinal,enabled FROM chunks WHERE dataset_id=? ORDER BY id", (identifier,)))
        try:
            configurations = [self.models.find(model_id, "chat", private=True), self.models.find("", "rerank", private=True)]
            configurations.extend(self.models.find(dataset["embedding_model"], "embedding", private=True) for dataset in material["datasets"])
        except HTTPException:
            raise CollabStale() from None
        # API keys participate in the fingerprint in memory only; never saved or returned.
        return _digest(material), _digest(configurations)

    def _assert_context(self, row):
        request = json.loads(row["request"])
        if tuple(request["signatures"]) != self._context_signature(row["chat_id"], request["dataset_ids"], row["model_id"]):
            raise CollabStale()
        session = self.db.one("SELECT messages FROM sessions WHERE id=? AND chat_id=?", (row["session_id"], row["chat_id"]))
        messages = json.loads(session["messages"]) if session else []
        count = request.get("history_count", len(request["history"]))
        if len(messages) != count + 2 or _digest(messages[:count]) != request["history_signature"] or messages[count].get("id") != row["user_message_id"] or messages[count].get("content") != request["question"] or messages[count].get("image") != request.get("image") or messages[count + 1].get("id") != row["assistant_message_id"]:
            raise CollabStale()

    def check_stop(self, state):
        from .collab_graph import CollabStopped
        row = self._row(state["job_id"])
        if row["worker_token"] != self._instance_token:
            raise CollabSuperseded()
        if self._closing or row["stop_requested"] or row["status"] in {"stopped", "cancelled"}:
            raise CollabStopped()

    def before_node(self, state, node):
        self.check_stop(state)
        self._assert_context(self._row(state["job_id"]))

    def assert_session_idle(self, session_id, *, deleting=False):
        row = self.db.one("SELECT id,status FROM collab_jobs WHERE session_id=? AND status IN ('pending','running','stopping','waiting_confirmation','stopped','failed') ORDER BY created_at DESC LIMIT 1", (session_id,))
        if row and (not deleting or row["status"] in RUNNING | {"waiting_confirmation"} or self._futures.get(row["id"]) and not self._futures[row["id"]].done()):
            raise HTTPException(409, "该会话有未结束的协审任务，请继续、确认或结束该任务后再操作")

    def create(self, payload, owner):
        with self._lock:
            if self._closing:
                raise HTTPException(503, "服务正在关闭，请稍后再试")
            if not isinstance(payload, dict):
                raise HTTPException(400, "协审请求格式无效")
            question = payload.get("question", "")
            if not isinstance(question, str) or not question.strip() or len(question) > 16000:
                raise HTTPException(400, "协审问题不能为空，且不得超过 16000 字")
            question = question.strip()
            image, image_bytes = decode_image(payload.get("image"))
            require_confirmation = payload.get("require_confirmation", False)
            if type(require_confirmation) is not bool:
                raise HTTPException(400, "人工确认选项必须为布尔值")
            if not isinstance(payload.get("chat_id"), str) or not payload["chat_id"] or len(payload["chat_id"]) > 200 or payload.get("session_id") is not None and (not isinstance(payload["session_id"], str) or len(payload["session_id"]) > 200):
                raise HTTPException(400, "助手或会话编号无效")
            for field in ("model_id", "llm_id"):
                if field in payload and (not isinstance(payload[field], str) or len(payload[field]) > 1000):
                    raise HTTPException(400, "模型编号无效")
            model_choice = payload.get("model_id") or payload.get("llm_id") or ""
            if not isinstance(model_choice, str) or len(model_choice) > 1000:
                raise HTTPException(400, "模型编号无效")
            request_id = payload.get("request_id")
            if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 200):
                raise HTTPException(400, "请求编号无效")
            if request_id:
                previous = self.db.one("SELECT * FROM collab_jobs WHERE owner_id=? AND request_id=?", (owner, request_id))
                if previous:
                    old = json.loads(previous["request"])
                    old_image = {key: value for key, value in old.get("image", {}).items() if key != "id"}
                    if old["question"] != question or previous["chat_id"] != payload["chat_id"] or payload.get("session_id") and previous["session_id"] != payload["session_id"] or old.get("client_model", "") != model_choice or old["settings"]["require_confirmation"] != require_confirmation or old_image != (image or {}):
                        raise HTTPException(409, "同一请求编号不能用于另一条协审问题")
                    return self._public(previous)
            assistant = self.db.json_get("chats", payload.get("chat_id", ""))
            if not assistant:
                raise HTTPException(404, "审查助手不存在")
            model = self.models.find(payload.get("model_id") or payload.get("llm_id") or assistant.get("llm_id") or "", "chat")
            dataset_ids = assistant.get("dataset_ids", [])
            if not isinstance(dataset_ids, list) or len(dataset_ids) > 50:
                raise HTTPException(400, "助手关联知识库配置无效")
            for dataset_id in dataset_ids:
                self.knowledge.dataset(dataset_id)
            threshold = float(assistant.get("similarity_threshold", .15))
            if not math.isfinite(threshold) or not 0 <= threshold <= 1:
                raise HTTPException(400, "助手检索阈值配置无效")
            session_id = payload.get("session_id") or uid()
            existing = self.db.one("SELECT * FROM sessions WHERE id=? AND chat_id=?", (session_id, assistant["id"]))
            if payload.get("session_id") and not existing:
                raise HTTPException(404, "会话不存在或不属于此助手")
            if existing:
                self.assert_session_idle(session_id)
            history = json.loads(existing["messages"]) if existing else []
            if len(dumps(history).encode()) > 12 * 1024 * 1024:
                raise HTTPException(413, "会话记录过长，请新建会话后继续协审")
            signatures = self._context_signature(assistant["id"], dataset_ids, model["id"])
            identifier, user_id, answer_id, timestamp = uid(), uid(), uid(), now()
            bounded_history = [{"role": item.get("role"), "content": str(item.get("content", ""))[:6000], "replyState": item.get("replyState", "complete")} for item in history[-20:]]
            request = {"question": question, "history": bounded_history, "history_count": len(history), "history_signature": _digest(history),
                "dataset_ids": dataset_ids, "model_id": model["id"], "settings": {"similarity_threshold": threshold, "require_confirmation": require_confirmation},
                "signatures": signatures, "client_model": model_choice}
            messages = [*history, {"id": user_id, "role": "user", "content": question},
                {"id": answer_id, "role": "assistant", "content": "", "replyState": "running", "reference": {"chunks": []}, "workflow": {"job_id": identifier, "status": "pending"}}]
            if image:
                request["image"] = messages[-2]["image"] = {"id": user_id, **image}
            with self.db.transaction() as connection:
                if not connection.execute("SELECT id FROM chats WHERE id=?", (assistant["id"],)).fetchone():
                    raise HTTPException(404, "审查助手已被删除，未创建任务")
                if existing:
                    current = connection.execute("SELECT messages FROM sessions WHERE id=?", (session_id,)).fetchone()
                    if not current or current["messages"] != existing["messages"]:
                        raise HTTPException(409, "会话已改变，请刷新后重试")
                    if connection.execute("SELECT id FROM collab_jobs WHERE session_id=? AND status IN ('pending','running','stopping','waiting_confirmation','stopped','failed')", (session_id,)).fetchone():
                        raise HTTPException(409, "该会话已有未结束的协审任务")
                    connection.execute("UPDATE sessions SET messages=?,revision=revision+1,updated_at=? WHERE id=?", (dumps(messages), timestamp, session_id))
                else:
                    connection.execute("INSERT INTO sessions(id,chat_id,name,messages,created_at,updated_at) VALUES (?,?,?,?,?,?)", (session_id, assistant["id"], question[:60], dumps(messages), timestamp, timestamp))
                if image:
                    connection.execute("INSERT INTO collab_images(id,session_id,media_type,content) VALUES (?,?,?,?)",
                        (user_id, session_id, image["media_type"], image_bytes))
                connection.execute("INSERT INTO collab_jobs(id,owner_id,session_id,chat_id,model_id,workflow_version,status,phase,message,request,user_message_id,assistant_message_id,request_id,worker_token,created_at,updated_at) VALUES (?,?,?,?,?,?,'pending','queued',?,?,?,?,?,?,?,?)",
                    (identifier, owner, session_id, assistant["id"], model["id"], self.workflow_version, "问题已保存，准备执行协审。", dumps(request), user_id, answer_id, request_id, self._instance_token, timestamp, timestamp))
                self._event(connection, identifier, "queued", "state", {"status": "pending", "message": "问题已保存，准备执行协审。"})
            self._submit(identifier, self._initial(self._row(identifier)))
            return self.get(identifier, owner)

    def _initial(self, row):
        request = json.loads(row["request"])
        return {"job_id": row["id"], "question": request["question"], "history": request["history"],
            **({"image": request["image"]} if request.get("image") else {}),
            "dataset_ids": request["dataset_ids"], "model_id": row["model_id"], "settings": request["settings"],
            "references": [], "answer": "", "retrieval_rounds": 0, "auto_rounds": 0, "manual_rounds": 0, "answer_attempts": 0}

    def _submit(self, identifier, graph_input):
        with self._lock:
            if self._closing:
                return
            previous = self._futures.get(identifier)
            if previous and not previous.done():
                self._queued[identifier] = graph_input
                return
            future = self._executor.submit(self._run, identifier, graph_input)
            self._futures[identifier] = future
            def finished(done):
                with self._lock:
                    if self._futures.get(identifier) is not done:
                        return
                    self._futures.pop(identifier, None)
                    self._delta_clock.pop(identifier, None)
                    if identifier in self._queued and not self._closing:
                        pending = self._queued.pop(identifier)
                        if self._row(identifier)["status"] == "pending":
                            self._submit(identifier, pending)
                    if self._closing and not self._futures and not self._checkpoint_closed:
                        self._checkpoint_connection.close()
                        self._checkpoint_closed = True
            future.add_done_callback(finished)

    def _run(self, identifier, graph_input):
        from .collab_graph import CollabOutputError, CollabStopped
        answer, changed = "", False
        self._worker_local.job_id = identifier
        try:
            with self._lock:
                row = self._row(identifier)
                if row["status"] in TERMINAL:
                    return
                self.check_stop({"job_id": identifier})
                self._assert_context(row)
                answer = json.loads(row["result"]).get("answer", "")
                self._update(identifier, status="running", node="started", message="协审流程正在后台运行。")
            config = self._config(identifier)
            for mode, event in self.graph.stream(graph_input, config, stream_mode=["updates", "custom"]):
                if mode == "custom":
                    if event.get("type") == "delta":
                        answer += str(event.get("delta", ""))
                        changed = True
                        if time.monotonic() - self._delta_clock.get(identifier, 0) >= .12:
                            self._update(identifier, result_patch={"answer": answer}, node="answer_stream", kind="delta")
                            self._delta_clock[identifier] = time.monotonic()
                            changed = False
                    elif event.get("type") == "replace":
                        answer = str(event.get("answer", ""))
                        self._update(identifier, result_patch={"answer": answer}, node="answer_revision")
                    elif event.get("type") == "stage":
                        self._update(identifier, phase=str(event.get("node", "processing")), message=event.get("message", "正在处理"), node=str(event.get("node", "processing")))
                elif mode == "updates" and isinstance(event, dict):
                    for node, update in event.items():
                        if str(node).startswith("__") or not isinstance(update, dict):
                            continue
                        self._assert_context(self._row(identifier))
                        if isinstance(update.get("answer"), str):
                            answer = update["answer"]
                        self._update(identifier, result_patch=update, node=str(node))
                        changed = False
            with self._lock:
                snapshot = self.graph.get_state(config)
                if snapshot.values:
                    self._update(identifier, result_patch=snapshot.values, node="checkpoint_saved")
                self.check_stop({"job_id": identifier})
                self._assert_context(self._row(identifier))
                if self._interrupt_ids(snapshot):
                    self._update(identifier, status="waiting_confirmation", phase="human_review", pending_command="", node="waiting_confirmation", message="协审草稿与依据已保存，请核对并确认、修改或补充检索。")
                elif snapshot.next:
                    self._update(identifier, status="stopped", pending_command="", node="paused", message="协审检查点已保存，可以继续处理。")
                else:
                    self._update(identifier, status="completed", phase="finalize", pending_command="", node="completed", message="协审结果已保存。")
        except CollabSuperseded:
            return
        except Exception as error:
            try:
                if changed:
                    self._update(identifier, result_patch={"answer": answer}, node="partial_answer_saved", kind="delta")
                if isinstance(error, CollabStopped):
                    row = self._row(identifier)
                    cancelled = row["stop_requested"] == 2 or row["status"] == "cancelled"
                    self._update(identifier, status="cancelled" if cancelled else "stopped", stop_requested=0, node="cancelled" if cancelled else "stopped",
                        message="本次协审已结束，未确认草稿不作为正式复核结论。" if cancelled else "协审已暂停，检查点和已生成内容已保留。")
                elif isinstance(error, CollabStale):
                    self._update(identifier, status="stale", stop_requested=0, node="context_changed", message="材料、模型、助手配置或会话内容已改变，请保留已有记录并发起新的协审。")
                else:
                    LOGGER.warning("Collaborative job %s failed (%s)", identifier, type(error).__name__)
                    message = str(error)[:500] if isinstance(error, CollabOutputError) else provider_error(error)
                    if getattr(error, "status_code", None) in {400, 413, 415, 422} and json.loads(self._row(identifier)["request"]).get("image"):
                        message += " 本轮含图片，请确认所选模型及接入接口支持图片输入，并检查尺寸限制。"
                    self._update(identifier, status="failed", stop_requested=0, node="failed", message=message)
            except CollabSuperseded:
                return
            except HTTPException as missing:
                if missing.status_code != 404:
                    raise
        finally:
            self._worker_local.job_id = None

    @staticmethod
    def _interrupt_ids(snapshot):
        values = list(getattr(snapshot, "interrupts", ()) or ())
        values.extend(interrupt for task in getattr(snapshot, "tasks", ()) for interrupt in getattr(task, "interrupts", ()))
        return sorted({str(getattr(interrupt, "id", "unknown")) for interrupt in values})

    def _command_row(self, identifier, owner, body, allowed, *, stopping=False):
        row = self._row(identifier, owner)
        if stopping:
            # Stop/cancel intent remains valid as tokens arrive. Approval and
            # resume still require the exact revision of the reviewed state.
            revision = body.get("revision")
            if type(revision) is not int or not 1 <= revision <= row["revision"]:
                self._revision(row, revision)
        else:
            self._revision(row, body.get("revision"))
        if self._closing:
            raise HTTPException(503, "服务正在关闭，请稍后再试")
        if row["workflow_version"] != self.workflow_version:
            raise HTTPException(409, "该任务使用旧版协审流程，请结束后新建任务")
        if row["status"] not in allowed:
            raise HTTPException(409, "当前协审状态不允许此操作，请刷新后重试")
        return row

    def _resume_context(self, row):
        try:
            self._assert_context(row)
        except CollabStale:
            self._update(row["id"], status="stale", node="context_changed", message="材料、模型或会话内容已改变，请新建协审任务。")
            raise HTTPException(409, "材料、模型或会话内容已改变，请新建协审任务") from None

    def stop(self, identifier, owner, body):
        with self._lock:
            row = self._command_row(identifier, owner, body, RUNNING | {"waiting_confirmation"}, stopping=True)
            self._update(identifier, status="stopping" if row["status"] in RUNNING else "stopped", stop_requested=1, node="stop_requested", message="已请求暂停，当前处理结束后保存检查点。")
            return self.get(identifier, owner)

    def resume(self, identifier, owner, body):
        with self._lock:
            row = self._command_row(identifier, owner, body, {"stopped", "failed"})
            self._resume_context(row)
            snapshot = self.graph.get_state(self._config(identifier))
            if snapshot.values and snapshot.values.get("job_id") != identifier:
                raise HTTPException(409, "协审检查点标识无效，请结束后新建任务")
            graph_input = None if snapshot.values else self._initial(row)
            if row["pending_command"]:
                pending = json.loads(row["pending_command"])
                if self._interrupt_ids(snapshot) and self._interrupt_ids(snapshot) == pending.get("interrupt_ids"):
                    graph_input = Command(resume=pending["decision"])
            self._update(identifier, status="pending", stop_requested=0, worker_token=self._instance_token, node="resume_requested", message="已请求继续，将从保存的协审检查点恢复。")
            self._submit(identifier, graph_input)
            return self.get(identifier, owner)

    def review(self, identifier, owner, body):
        from .collab_graph import validate_review
        with self._lock:
            row = self._command_row(identifier, owner, body, {"waiting_confirmation"})
            self._resume_context(row)
            snapshot = self.graph.get_state(self._config(identifier))
            if not snapshot.values or snapshot.values.get("job_id") != identifier:
                raise HTTPException(409, "缺少有效的协审确认检查点")
            try:
                decision = validate_review(snapshot.values, body)
            except (ValueError, TypeError, KeyError) as error:
                raise HTTPException(400, str(error)[:500] or "复核提交格式无效") from None
            decision = {key: decision[key] for key in ("decision", "answer", "notes", "query") if key in decision}
            decision["actor_id"], decision["reviewed_at"] = owner, now()
            pending = {"decision": decision, "interrupt_ids": self._interrupt_ids(snapshot)}
            with self.db.transaction() as connection:
                current = connection.execute("SELECT revision,status FROM collab_jobs WHERE id=?", (identifier,)).fetchone()
                if not current or current["revision"] != row["revision"] or current["status"] != "waiting_confirmation":
                    raise HTTPException(409, "协审草稿已改变，请刷新后重新核对")
                connection.execute("INSERT INTO collab_review_decisions(id,job_id,owner_id,decision,revision,draft,final_text,notes,query,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (uid(), identifier, owner, decision["decision"], row["revision"], snapshot.values.get("answer", ""), decision.get("answer", snapshot.values.get("answer", "")), decision.get("notes", ""), decision.get("query", ""), now()))
                connection.execute("UPDATE collab_jobs SET status='pending',stop_requested=0,worker_token=?,pending_command=?,revision=revision+1,message=?,updated_at=? WHERE id=? AND revision=?", (self._instance_token, dumps(pending), "人工复核意见已保存，继续处理。", now(), identifier, row["revision"]))
                self._event(connection, identifier, "human_decision", "state", {"status": "pending", "message": "人工复核意见已保存。", "decision": decision["decision"]})
                self._sync_message(connection, identifier)
            self._submit(identifier, Command(resume=decision))
            return self.get(identifier, owner)

    def cancel(self, identifier, owner, body):
        with self._lock:
            row = self._command_row(identifier, owner, body, OPEN | {"stale"}, stopping=True)
            active = row["status"] in RUNNING or bool(self._futures.get(identifier) and not self._futures[identifier].done())
            self._queued.pop(identifier, None)
            self._update(identifier, status="stopping" if active else "cancelled", stop_requested=2 if active else 0, pending_command="", node="cancel_requested",
                message="正在结束本次协审；未确认内容保留为草稿。" if active else "本次协审已结束；未确认内容保留为草稿。")
            return self.get(identifier, owner)

    def forget_jobs(self, identifiers):
        # Called after deleting idle sessions; application rows cascade first.
        with self._lock:
            for identifier in identifiers:
                self.checkpointer.delete_thread(identifier)

    def graph_description(self):
        graph = self.graph.get_graph()
        return {"workflow_version": self.workflow_version, "mermaid": graph.draw_mermaid(),
            "nodes": [{"id": node.id} for node in graph.nodes.values()],
            "edges": [{"source": edge.source, "target": edge.target, "conditional": bool(edge.conditional)} for edge in graph.edges],
            "limits": {"automatic_retrievals": 3, "human_supplements": 2}}

    def close(self):
        with self._lock:
            self._closing = True
            for row in self.db.rows("SELECT id FROM collab_jobs WHERE status IN ('pending','running','stopping') AND worker_token=?", (self._instance_token,)):
                self._update(row["id"], status="stopped", stop_requested=1, expected_worker_token=self._instance_token, node="service_shutdown", message="服务正在关闭，已保留协审进度；重启后手动继续。")
            self._executor.shutdown(wait=False, cancel_futures=True)
            if not self._futures and not self._checkpoint_closed:
                self._checkpoint_connection.close()
                self._checkpoint_closed = True


def make_collab_jobs_router(service):
    router = APIRouter(prefix="/api/v1/collab-jobs", tags=["collaborative-review-workflow"])

    @router.get("")
    def list_jobs(session_id: str = "", chat_id: str = "", owner: str = Depends(require_user)):
        return service.list(owner, session_id, chat_id)

    @router.post("")
    def create(body: dict = Body(...), owner: str = Depends(require_user)):
        return service.create(body, owner)

    @router.get("/images/{identifier}")
    def image(identifier: str, owner: str = Depends(require_user)):
        row = read_image(service.db, identifier)
        return Response(row["content"], media_type=row["media_type"])

    @router.get("/graph")
    def graph(owner: str = Depends(require_user)):
        return service.graph_description()

    @router.get("/{identifier}")
    def get(identifier: str, owner: str = Depends(require_user)):
        return service.get(identifier, owner)

    @router.get("/{identifier}/decisions")
    def decisions(identifier: str, owner: str = Depends(require_user)):
        return service.decisions(identifier, owner)

    @router.post("/{identifier}/stop")
    def stop(identifier: str, body: dict = Body(...), owner: str = Depends(require_user)):
        return service.stop(identifier, owner, body)

    @router.post("/{identifier}/resume")
    def resume(identifier: str, body: dict = Body(...), owner: str = Depends(require_user)):
        return service.resume(identifier, owner, body)

    @router.post("/{identifier}/review")
    def review(identifier: str, body: dict = Body(...), owner: str = Depends(require_user)):
        return service.review(identifier, owner, body)

    @router.post("/{identifier}/cancel")
    def cancel(identifier: str, body: dict = Body(...), owner: str = Depends(require_user)):
        return service.cancel(identifier, owner, body)

    @router.get("/{identifier}/events")
    async def events(identifier: str, request: Request, after: int = Query(0, ge=0, le=2**63-1), owner: str = Depends(require_user)):
        service.get(identifier, owner)
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
            if not 0 <= cursor <= 2**63-1:
                raise ValueError()
        except ValueError:
            raise HTTPException(400, "协审事件游标无效") from None
        async def generate():
            nonlocal cursor
            heartbeat, first = time.monotonic(), True
            while not await request.is_disconnected():
                try:
                    job = await asyncio.to_thread(service.get, identifier, owner)
                except HTTPException:
                    return
                if first or job["last_event_id"] > cursor:
                    cursor, first = job["last_event_id"], False
                    yield "id: " + str(cursor) + "\ndata: " + dumps({"type": "state", "job": job, "cursor": cursor}) + "\n\n"
                    heartbeat = time.monotonic()
                if job["status"] not in RUNNING:
                    return
                if time.monotonic() - heartbeat > 10:
                    yield ": keep-alive\n\n"
                    heartbeat = time.monotonic()
                await asyncio.sleep(.15)
        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return router
