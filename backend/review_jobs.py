"""Durable, owner-scoped runners for the quick-review LangGraph workflow.

The worker owns execution and report writes. HTTP/SSE connections only observe
that worker; disconnecting a browser never cancels a model request or a job.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
import copy
import json
import logging
import re
import sqlite3
import threading
import time

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Command

from .db import dumps, now, uid
from .security import require_user


LOGGER = logging.getLogger(__name__)
WORKFLOW_VERSION = "quick-review-v1"
RUNNING = {"pending", "running", "stopping"}
RESUMABLE = {"stopped", "failed"}
ACTIVE = RUNNING | RESUMABLE | {"waiting_confirmation"}
STATES = ACTIVE | {"completed", "stale"}


def _node_label(identifier, fallback=None):
    try:
        from .review_graph import NODE_LABELS
    except ImportError:
        NODE_LABELS = {}
    leaf = str(identifier).replace(":", "/").split("/")[-1]
    return NODE_LABELS.get(leaf, fallback or leaf)


def _safe_failure_message(error):
    fallback = "快速审查未完成，请检查模型配置与任务处理记录后继续。"
    if type(error).__name__ != "ReviewFatalError":
        return fallback
    reason = str(error).strip()
    unsafe = re.search(r"https?://|www\.|api[_ -]?key|authorization|bearer\s|sk-[a-z0-9]|密钥\s*[:：=]|[A-Za-z0-9_-]{24,}", reason, re.I)
    if reason and len(reason) <= 300 and re.search(r"[\u4e00-\u9fff]", reason) and not unsafe:
        return reason
    return fallback


class ReviewJobService:
    def __init__(self, db, models, knowledge, retrieval, records, settings, *,
                 engine=None, graph_factory=None, max_workers=2):
        self.db, self.models, self.knowledge = db, models, knowledge
        self.retrieval, self.records, self.settings = retrieval, records, settings
        self._lock = threading.RLock()
        self._futures: dict[str, Future] = {}
        self._closing = False
        self._checkpoint_closed = False
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="review-workflow")
        with db.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS review_jobs (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                    workflow_version TEXT NOT NULL, status TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    stage INTEGER NOT NULL DEFAULT 1, phase TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '', request TEXT NOT NULL,
                    report TEXT NOT NULL DEFAULT '{}', record_id TEXT,
                    record_revision INTEGER, stop_requested INTEGER NOT NULL DEFAULT 0,
                    last_event_id INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS review_jobs_owner ON review_jobs(owner_id, updated_at);
                CREATE UNIQUE INDEX IF NOT EXISTS review_jobs_record ON review_jobs(record_id) WHERE record_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS review_job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    node TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES review_jobs(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS review_job_events_job ON review_job_events(job_id, id);
            """)
        if engine is None:
            from .review_engine import ReviewEngine
            engine = ReviewEngine(db, models, knowledge, retrieval, settings)
        if graph_factory is None:
            from .review_graph import WORKFLOW_VERSION as version, build_review_graph
            self.workflow_version = version
            graph_factory = build_review_graph
        else:
            self.workflow_version = WORKFLOW_VERSION
        self.engine = engine
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._checkpoint_connection = sqlite3.connect(
            str(settings.data_dir / "review-checkpoints.sqlite3"), check_same_thread=False, timeout=30)
        self._checkpoint_connection.execute("PRAGMA journal_mode=WAL")
        self._checkpoint_connection.execute("PRAGMA busy_timeout=30000")
        self.checkpointer = SqliteSaver(self._checkpoint_connection,
                                       serde=JsonPlusSerializer(allowed_msgpack_modules=[]))
        self.checkpointer.setup()
        self.graph = graph_factory(engine, self.checkpointer, self)
        # Starting the application must never silently make paid model calls.
        for row in db.rows("SELECT id FROM review_jobs WHERE status IN ('pending','running','stopping')"):
            self._update(row["id"], status="stopped", stop_requested=0, node="service_restart",
                         message="服务已重新启动，检查点和已完成结果已保留；请点击继续审查。")

    def _row(self, identifier, owner=None):
        row = self.db.one("SELECT * FROM review_jobs WHERE id=?", (identifier,))
        if not row or (owner is not None and row["owner_id"] != owner):
            raise HTTPException(404, "审查任务不存在")
        return row

    @staticmethod
    def _revision(row, revision):
        if type(revision) is not int or revision != row["revision"]:
            raise HTTPException(409, "任务状态已变化，请刷新任务后重试")

    def _current_record(self, row):
        return self.db.one("SELECT revision,report FROM review_records WHERE id=?", (row["record_id"],)) if row["record_id"] else None

    def _record_unchanged(self, row):
        record = self._current_record(row)
        if row["record_id"] and (not record or record["revision"] != row["record_revision"]):
            raise HTTPException(409, "报告记录已被人工修改或删除，请新建任务，避免覆盖已有修订")

    def _public(self, row, *, detail=True):
        report = json.loads(row["report"])
        record = self._current_record(row)
        modified = bool(row["record_id"] and (not record or record["revision"] != row["record_revision"]))
        compatible = row["workflow_version"] == self.workflow_version
        if modified and record and row["status"] not in RUNNING:
            report = json.loads(record["report"])
        can_retry = bool(compatible and not modified and row["status"] == "completed" and
                         (report.get("workflowCanRetry") is True or
                          any(item.get("retryable") for item in report.get("results", []) if isinstance(item, dict))))
        value = {"id": row["id"], "task_id": row["id"], "workflow_version": row["workflow_version"],
                 "status": row["status"], "revision": row["revision"], "stage": row["stage"], "phase": row["phase"],
                 "message": row["message"], "record_id": row["record_id"], "record_revision": record["revision"] if modified and record else row["record_revision"],
                 "last_event_id": row["last_event_id"], "created_at": row["created_at"], "updated_at": row["updated_at"],
                 "can_resume": compatible and not modified and row["status"] in (RESUMABLE | {"waiting_confirmation"})
                               and not (row["status"] == "failed" and row["phase"] == "failed"),
                 "can_retry": can_retry, "record_modified": modified, "workflow_compatible": compatible,
                 "requirements_count": len(report.get("requirements", [])), "results_count": len(report.get("results", []))}
        if detail:
            value["report"] = report
        else:
            value["title"] = report.get("title", "合同与技术协议审查")
        return value

    def get(self, identifier, owner):
        with self._lock:
            return self._public(self._row(identifier, owner))

    def list(self, owner, active=False):
        with self._lock:
            rows = self.db.rows("SELECT * FROM review_jobs WHERE owner_id=? ORDER BY updated_at DESC LIMIT 100", (owner,))
            values = [self._public(row, detail=False) for row in rows]
            return [value for value in values if not active or value["status"] in RUNNING or value["can_resume"]]

    @staticmethod
    def _event(connection, row, node):
        report = json.loads(row["report"])
        payload = {"job_id": row["id"], "revision": row["revision"], "node": str(node)[:200], "label": _node_label(node),
                   "status": row["status"], "stage": row["stage"], "phase": row["phase"], "message": row["message"],
                   "requirements_count": len(report.get("requirements", [])), "results_count": len(report.get("results", [])),
                   "created_at": row["updated_at"]}
        cursor = connection.execute("INSERT INTO review_job_events(job_id,node,payload,created_at) VALUES (?,?,?,?)",
                                    (row["id"], str(node)[:200], dumps(payload), row["updated_at"]))
        connection.execute("UPDATE review_jobs SET last_event_id=? WHERE id=?", (cursor.lastrowid, row["id"]))

    def _update(self, identifier, *, node, **fields):
        allowed = {"status", "stage", "phase", "message", "report", "record_id", "record_revision", "stop_requested"}
        fields = {key: value for key, value in fields.items() if key in allowed}
        if "status" in fields and fields["status"] not in STATES:
            raise ValueError("Invalid workflow status")
        if isinstance(fields.get("report"), dict):
            fields["report"] = dumps(fields["report"])
        with self._lock, self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM review_jobs WHERE id=?", (identifier,)).fetchone()
            if not row:
                return
            if fields:
                fields["updated_at"] = now()
                columns = ",".join(key + "=?" for key in fields)
                connection.execute("UPDATE review_jobs SET " + columns + ",revision=revision+1 WHERE id=?",
                                   (*fields.values(), identifier))
            fresh = dict(connection.execute("SELECT * FROM review_jobs WHERE id=?", (identifier,)).fetchone())
            self._event(connection, fresh, node)

    def _validate_request(self, payload):
        if not isinstance(payload, dict):
            raise HTTPException(400, "任务输入无效")
        kb = str(payload.get("kb") or "").strip()
        self.knowledge.dataset(kb)
        identifiers = payload.get("baseline_document_ids")
        if not isinstance(identifiers, list) or not identifiers or len(identifiers) > 20 or any(not isinstance(item, str) for item in identifiers):
            raise HTTPException(400, "请选择 1–20 份合同或技术协议")
        identifiers = list(dict.fromkeys(identifiers))
        baseline = [self.knowledge.document(identifier, kb) for identifier in identifiers]
        scope = payload.get("scope", "dataset")
        if scope not in {"dataset", "files"}:
            raise HTTPException(400, "审查范围无效")
        targets = payload.get("target_document_ids", [])
        if not isinstance(targets, list) or len(targets) > 1000 or any(not isinstance(item, str) for item in targets):
            raise HTTPException(400, "待审文件范围无效")
        targets = [item for item in dict.fromkeys(targets) if item not in identifiers]
        target_docs = [self.knowledge.document(identifier, kb) for identifier in targets] if scope == "files" else []
        if scope == "files" and not target_docs:
            raise HTTPException(400, "限定范围至少需要一份有效待审文件")
        model = self.models.find(str(payload.get("model_id") or ""), "chat")
        title, focus = payload.get("title", "合同与技术协议审查"), payload.get("focus", "")
        if not isinstance(title, str) or not title.strip() or len(title) > 200 or not isinstance(focus, str) or len(focus) > 4000:
            raise HTTPException(400, "任务名称或审查重点长度无效")
        request = {"kb": kb, "baseline_document_ids": identifiers, "scope": scope,
                   "target_document_ids": targets if scope == "files" else [], "model_id": model["id"],
                   "title": title.strip(), "focus": focus}
        vision_id = payload.get("extraction_vision_model_id")
        if any(json.loads(row.get("metadata", "{}")).get("review_basis") for row in baseline) and not vision_id:
            raise HTTPException(400, "请选择指标提取视觉模型。")
        if vision_id:
            request["extraction_vision_model_id"] = self.models.vision_config(str(vision_id))["id"]
        return request, baseline, target_docs, model

    def create(self, owner, payload):
        with self._lock:
            if self._closing:
                raise HTTPException(503, "服务正在关闭，请稍后再试")
            running = self.db.one("SELECT COUNT(*) AS count FROM review_jobs WHERE owner_id=? AND status IN ('pending','running','stopping')", (owner,))["count"]
            if running >= 8:
                raise HTTPException(409, "已有较多正在执行或排队的任务，请先停止部分任务后再新建")
            request, baseline, targets, model = self._validate_request(payload)
            identifier = uid()
            report = {"version": 4, "mode": "three-stage", "scope": request["scope"], "kb": request["kb"],
                      "kbName": self.knowledge.dataset(request["kb"])["name"], "title": request["title"],
                      "createdAt": now(), "baselineDocs": [{"id": item["id"], "name": item["name"]} for item in baseline],
                      "targetDocs": [{"id": item["id"], "name": item["name"]} for item in targets],
                      "model": model["id"], "modelLabel": model.get("name", model["id"]), "focus": request["focus"],
                      "requirements": [], "results": [], "issues": [], "complete": False,
                      "workflowJobId": identifier, "workflowVersion": self.workflow_version}
            if request.get("extraction_vision_model_id"):
                visual = self.models.vision_config(request["extraction_vision_model_id"])
                report.update(extractionVisionModel=visual["id"], extractionVisionLabel=visual["name"])
            record_id = payload.get("record_id")
            body = {"report": report, "status": "未开始"}
            if record_id:
                old = self.records.get(str(record_id))
                if old["report"].get("results"):
                    raise HTTPException(409, "旧报告已有核查结果，请保留原记录并新建审查任务")
                if self.db.one("SELECT id FROM review_jobs WHERE record_id=?", (record_id,)):
                    raise HTTPException(409, "该记录已关联工作流，请打开对应任务继续")
                if type(payload.get("record_revision")) is not int or payload["record_revision"] != old["revision"]:
                    raise HTTPException(409, "记录版本已变化，请重新打开保存任务")
                body["revision"] = old["revision"]
            saved = self.records.save(body, str(record_id) if record_id else None, _workflow=True)
            report["dbId"], report["dbRevision"] = saved["id"], saved["revision"]
            timestamp = now()
            with self.db.transaction() as connection:
                connection.execute("INSERT INTO review_jobs(id,owner_id,workflow_version,status,request,report,record_id,record_revision,message,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (identifier, owner, self.workflow_version, "pending", dumps(request), dumps(report), saved["id"], saved["revision"], "任务已创建，等待后台执行。", timestamp, timestamp))
                row = dict(connection.execute("SELECT * FROM review_jobs WHERE id=?", (identifier,)).fetchone())
                self._event(connection, row, "created")
            self._submit(identifier, {"task_id": identifier, "request": request, "report": report})
            return self.get(identifier, owner)

    def should_stop(self, identifier):
        with self._lock:
            if self._closing:
                return True
        row = self.db.one("SELECT stop_requested FROM review_jobs WHERE id=?", (identifier,))
        return not row or bool(row["stop_requested"])

    def _submit(self, identifier, graph_input):
        if self._closing:
            raise HTTPException(503, "服务正在关闭，请稍后再试")
        previous = self._futures.get(identifier)
        # A terminal projection can reach the browser just before its worker
        # returns. A valid new CAS command queues behind that finishing worker;
        # it never starts two graph executions for one thread concurrently.
        def execute():
            if previous and not previous.done():
                try:
                    previous.result()
                except Exception:
                    pass
            self._run(identifier, graph_input)
        future = self._executor.submit(execute)
        self._futures[identifier] = future
        future.add_done_callback(lambda finished: self._finished(identifier, finished))

    def _finished(self, identifier, finished):
        with self._lock:
            if self._futures.get(identifier) is finished:
                self._futures.pop(identifier, None)
            if self._closing and not self._futures and not self._checkpoint_closed:
                self._checkpoint_connection.close()
                self._checkpoint_closed = True

    @staticmethod
    def _config(identifier):
        return {"configurable": {"thread_id": identifier}, "recursion_limit": 100_000}

    def _save_report(self, identifier, report):
        row = self._row(identifier)
        self._record_unchanged(row)
        report = copy.deepcopy(report)
        report["workflowJobId"], report["workflowVersion"] = identifier, self.workflow_version
        report["dbId"], report["dbRevision"] = row["record_id"], row["record_revision"] + 1
        legacy_status = "已完成" if report.get("complete") else "正在生成"
        saved = self.records.save({"report": report, "revision": row["record_revision"], "status": legacy_status}, row["record_id"], _workflow=True)
        return report, saved["id"], saved["revision"]

    def _project(self, identifier, node, update):
        fields = {key: update[key] for key in ("stage", "phase", "message") if key in update}
        if "stage" in fields:
            fields["stage"] = max(1, min(3, int(fields["stage"])))
        for key in ("phase", "message"):
            if key in fields:
                fields[key] = str(fields[key])[:2000]
        with self._lock:
            row = self._row(identifier)
            if isinstance(update.get("report"), dict) and dumps(update["report"]) != row["report"]:
                report, record_id, revision = self._save_report(identifier, update["report"])
                fields.update(report=report, record_id=record_id, record_revision=revision)
            self._update(identifier, node=node, **fields)

    @staticmethod
    def _interrupts(snapshot):
        found = list(getattr(snapshot, "interrupts", ()) or ())
        for task in getattr(snapshot, "tasks", ()):
            found.extend(getattr(task, "interrupts", ()) or ())
            nested = getattr(task, "state", None)
            if hasattr(nested, "tasks"):
                found.extend(ReviewJobService._interrupts(nested))
        return found

    def _run(self, identifier, graph_input):
        try:
            with self._lock:
                if self.should_stop(identifier):
                    self._update(identifier, status="stopped", node="stopped", message="任务已停止，已完成条目和检查点已保留。")
                    return
                self._record_unchanged(self._row(identifier))
                self._update(identifier, status="running", node="runner_started", message="后台工作流正在执行。")
            config = self._config(identifier)
            for chunk in self.graph.stream(graph_input, config, stream_mode="updates", subgraphs=True):
                namespace, updates = chunk if isinstance(chunk, tuple) and len(chunk) == 2 else ((), chunk)
                if not isinstance(updates, dict):
                    continue
                for node, update in updates.items():
                    if str(node).startswith("__") or not isinstance(update, dict):
                        continue
                    path = "/".join([*(str(part).split(":")[0] for part in namespace), str(node)])
                    self._project(identifier, path, update)
            with self._lock:
                snapshot = self.graph.get_state(config, subgraphs=True)
                if isinstance(snapshot.values, dict) and isinstance(snapshot.values.get("report"), dict):
                    self._project(identifier, "checkpoint_projection", snapshot.values)
                if self._closing or self._row(identifier)['stop_requested']:
                    self._update(identifier,status='stopped',node='stopped',message='任务已停止，检查点和已完成结果已保留。')
                    return
                interrupts = self._interrupts(snapshot)
                kinds = {item.value.get("type") for item in interrupts if isinstance(getattr(item, "value", None), dict)}
                if "confirm_requirements" in kinds:
                    self._update(identifier, status="waiting_confirmation", node="waiting_confirmation", message="指标已提取，请核对并确认后继续。")
                elif "retry_optional" in kinds:
                    self._update(identifier, status="completed", node="completed_with_retry", message="本轮处理结束，异常条目或报告整理可单独重试。")
                elif snapshot.next:
                    self._update(identifier, status="stopped", node="paused", message="流程已暂停，请查看说明后继续。")
                else:
                    row = self._row(identifier)
                    report = json.loads(row["report"])
                    if report.get("complete"):
                        self._update(identifier, status="completed", node="completed", message="逐项核查完成，报告已保存。")
                    else:
                        message = row["message"] if row["phase"] == "failed" and row["message"] else "本轮尚未形成完整报告，请检查处理记录后继续。"
                        self._update(identifier, status="failed", node="incomplete", message=message)
        except Exception as error:
            name = type(error).__name__
            if name == "ReviewStopped":
                message = "服务关闭前已保存检查点，请重新启动后继续。" if self._closing else "任务已停止，已完成条目和检查点已保留。"
                self._update(identifier, status="stopped", node="stopped", message=message)
            elif name in {"ReviewStale", "ReviewMaterialsChanged"} or isinstance(error, HTTPException) and error.status_code == 409:
                self._update(identifier, status="stale", node="materials_changed", message="资料、模型配置或报告记录已变更，请保留已有结果并新建审查任务。")
            else:
                # Never expose exception response bodies, URLs or credentials.
                LOGGER.warning("Quick review job %s failed (%s)", identifier, name)
                self._update(identifier, status="failed", node="failed", message=_safe_failure_message(error))

    def _command(self, identifier, owner, revision, allowed, *, status, message, stop_requested=0):
        row = self._row(identifier, owner)
        self._revision(row, revision)
        if row["workflow_version"] != self.workflow_version:
            raise HTTPException(409, "任务的工作流版本不同，请新建审查任务")
        if row["status"] not in allowed:
            raise HTTPException(409, "当前任务状态不允许此操作，请刷新后重试")
        self._record_unchanged(row)
        if self._closing:
            raise HTTPException(503, "服务正在关闭，请稍后再试")
        self._update(identifier, status=status, stop_requested=stop_requested, node="command", message=message)
        return row

    def _assert_checkpoint_materials(self, identifier, row, values):
        if not isinstance(values, dict):
            raise HTTPException(409, "检查点格式无效，请保留原记录并新建任务")
        if values and values.get("task_id") != identifier:
            raise HTTPException(409, "检查点与任务标识不一致，请保留原记录并新建任务")
        if values.get("material_signature") is None or values.get("model_signature") is None:
            return
        try:
            self.engine.assert_materials(json.loads(row["report"]), values["material_signature"], values["model_signature"])
        except Exception as error:
            if type(error).__name__ in {"ReviewMaterialsChanged", "ReviewStale"}:
                self._update(identifier, status="stale", node="materials_changed", message="资料或模型配置已变更，请新建审查任务。")
                raise HTTPException(409, "资料或模型配置已变更，请新建审查任务") from None
            raise

    def resume(self, identifier, owner, body):
        with self._lock:
            row = self._row(identifier, owner)
            self._revision(row, body.get("revision"))
            if row["workflow_version"] != self.workflow_version:
                raise HTTPException(409, "任务的工作流版本不同，请新建审查任务")
            if row["status"] == "failed" and row["phase"] == "failed":
                raise HTTPException(409, "本轮没有有效技术要求，请检查资料后新建审查任务")
            if row["status"] not in RESUMABLE | {"waiting_confirmation"}:
                raise HTTPException(409, "当前任务状态不允许继续，请刷新后重试")
            snapshot = self.graph.get_state(self._config(identifier), subgraphs=True)
            values = snapshot.values
            self._assert_checkpoint_materials(identifier, row, values)
            if row["status"] == "waiting_confirmation":
                submitted = body.get("requirements")
                if not isinstance(submitted, list) or not submitted or len(submitted) > 1000:
                    raise HTTPException(400, "请提交已确认的指标清单")
                report = json.loads(row["report"])
                # Validate outside interrupt before scheduling. The graph validates
                # again so direct service calls cannot bypass confirmation rules.
                try:
                    requirements = self.engine.confirm_requirements(report, submitted)
                except (ValueError, TypeError, KeyError):
                    raise HTTPException(400, "确认清单格式无效，请核对指标类型、名称、条件和来源") from None
                graph_input = Command(resume={"requirements": requirements})
            else:
                if "requirements" in body:
                    raise HTTPException(409, "只能在等待确认时修改要求；已有结果请新建修订任务")
                if not snapshot.values:
                    # A queued job may have been persisted before its worker
                    # wrote the first graph checkpoint. Only this explicit user
                    # command reconstructs its initial input after a restart.
                    graph_input = {"task_id": identifier, "request": json.loads(row["request"]),
                                   "report": json.loads(row["report"])}
                else:
                    if not isinstance(snapshot.values, dict) or snapshot.values.get("task_id") != identifier:
                        raise HTTPException(409, "检查点与任务标识不一致，请保留原记录并新建任务")
                    graph_input = None
            self._command(identifier, owner, body.get("revision"), RESUMABLE | {"waiting_confirmation"},
                          status="pending", message="已请求继续，将从持久检查点恢复。")
            self._submit(identifier, graph_input)
            return self.get(identifier, owner)

    def stop(self, identifier, owner, body):
        with self._lock:
            row = self._row(identifier, owner)
            revision = body.get("revision")
            if type(revision) is not int or not 1 <= revision <= row["revision"]:
                self._revision(row, revision)
            target = "stopping" if row["status"] in RUNNING else "stopped"
            # A stop request remains valid while progress advances. Human
            # confirmation/retry/resume continue to require their exact version.
            self._command(identifier, owner, row["revision"], RUNNING | {"waiting_confirmation"}, status=target,
                          message="正在停止；当前请求结束后会在节点边界保存并暂停。" if target == "stopping" else "任务已停止，确认清单和检查点已保留。", stop_requested=1)
            return self.get(identifier, owner)

    def retry(self, identifier, owner, body):
        with self._lock:
            row = self._row(identifier, owner)
            self._revision(row, body.get("revision"))
            if not self._public(row)["can_retry"]:
                raise HTTPException(409, "当前任务没有可重试的异常条目")
            snapshot = self.graph.get_state(self._config(identifier), subgraphs=True)
            self._assert_checkpoint_materials(identifier, row, snapshot.values)
            if not snapshot.values:
                raise HTTPException(409, "重试检查点不存在，请保留原报告并新建任务")
            self._command(identifier, owner, body.get("revision"), {"completed"}, status="pending", message="仅重试异常条目或报告整理，已成功核查结果将保留。")
            self._submit(identifier, Command(resume={"retry": True}))
            return self.get(identifier, owner)

    def events(self, identifier, owner, after=0, limit=200):
        self._row(identifier, owner)
        rows = self.db.rows("SELECT id,node,payload,created_at FROM review_job_events WHERE job_id=? AND id>? ORDER BY id LIMIT ?", (identifier, max(0, min(2**63-1, int(after))), max(1, min(500, int(limit)))))
        return [{"id": row["id"], **json.loads(row["payload"]), "label": _node_label(row["node"])} for row in rows]

    def graph_description(self, identifier=None, owner=None):
        if identifier is not None:
            row = self._row(identifier, owner)
            if row["workflow_version"] != self.workflow_version:
                raise HTTPException(409, "旧任务的工作流版本不同，不能使用当前编译图替代其执行结构")
        diagram = self.graph.get_graph(xray=True)
        value = {"workflow_version": self.workflow_version, "mermaid": diagram.draw_mermaid(),
                 "nodes": [{"id": str(key), "label": _node_label(key, str(getattr(node, "name", key)))} for key, node in diagram.nodes.items()],
                 "edges": [{"source": str(edge.source), "target": str(edge.target),
                            **({"label": str(edge.data)} if edge.data is not None else {}),
                            "conditional": bool(edge.conditional)} for edge in diagram.edges]}
        if identifier is not None:
            value["task_id"] = identifier
            value["trace"] = self.events(identifier, owner, 0, 500)
            value["trace_total"] = self.db.one("SELECT COUNT(*) AS count FROM review_job_events WHERE job_id=?", (identifier,))["count"]
            value["trace_truncated"] = value["trace_total"] > len(value["trace"])
        return value

    def forget_completed_record(self, record_id):
        """Remove only the inactive workflow for an already deleted record.

        RecordService is responsible for its normal revision/active-job guard.
        This hook runs after that deletion commits, so another record can never
        be removed by a guessed identifier or an interrupted cleanup.
        """
        with self._lock:
            if self.db.one("SELECT id FROM review_records WHERE id=?", (record_id,)):
                raise HTTPException(409, "请先完成报告记录删除，再清理其工作流")
            row = self.db.one("SELECT id,status FROM review_jobs WHERE record_id=?", (record_id,))
            if not row:
                return
            if row["status"] not in {"completed", "stopped", "failed", "stale"}:
                raise HTTPException(409, "执行中的工作流不能清理")
            self.checkpointer.delete_thread(row["id"])
            self.db.execute("DELETE FROM review_jobs WHERE id=? AND status IN ('completed','stopped','failed','stale')", (row["id"],))

    def close(self):
        with self._lock:
            self._closing = True
            for identifier, future in list(self._futures.items()):
                if future.cancel():
                    self._update(identifier, status="stopped", node="service_shutdown", stop_requested=0,
                                 message="服务关闭前保留了任务，重新启动后可继续。")
            self._executor.shutdown(wait=False, cancel_futures=True)
            if not self._futures and not self._checkpoint_closed:
                self._checkpoint_connection.close()
                self._checkpoint_closed = True


def make_review_jobs_router(service: ReviewJobService):
    router = APIRouter(prefix="/api/v1/review-jobs", tags=["quick-review-workflow"])

    @router.get("")
    def list_jobs(active: bool = False, owner: str = Depends(require_user)):
        return service.list(owner, active)

    @router.post("")
    def create_job(body: dict = Body(...), owner: str = Depends(require_user)):
        return service.create(owner, body)

    @router.get("/graph")
    def graph(owner: str = Depends(require_user)):
        return service.graph_description()

    @router.get("/{identifier}")
    def get_job(identifier: str, owner: str = Depends(require_user)):
        return service.get(identifier, owner)

    @router.get("/{identifier}/graph")
    def job_graph(identifier: str, owner: str = Depends(require_user)):
        return service.graph_description(identifier, owner)

    @router.post("/{identifier}/resume")
    def resume(identifier: str, body: dict = Body(...), owner: str = Depends(require_user)):
        return service.resume(identifier, owner, body)

    @router.post("/{identifier}/stop")
    def stop(identifier: str, body: dict = Body(...), owner: str = Depends(require_user)):
        return service.stop(identifier, owner, body)

    @router.post("/{identifier}/retry")
    def retry(identifier: str, body: dict = Body(...), owner: str = Depends(require_user)):
        return service.retry(identifier, owner, body)

    @router.get("/{identifier}/events")
    async def event_stream(identifier: str, request: Request, after: int = Query(0, ge=0, le=2**63-1), owner: str = Depends(require_user)):
        service.get(identifier, owner)
        try:
            cursor = max(after, max(0, int(request.headers.get("last-event-id", "0"))))
            if cursor > 2**63-1:
                raise ValueError("out of range")
        except ValueError:
            raise HTTPException(400, "事件游标无效")

        async def generate():
            nonlocal cursor
            last_heartbeat = time.monotonic()
            while not await request.is_disconnected():
                events = await asyncio.to_thread(service.events, identifier, owner, cursor)
                for event in events:
                    cursor = event["id"]
                    yield "id: " + str(cursor) + "\nevent: state\ndata: " + dumps(event) + "\n\n"
                    last_heartbeat = time.monotonic()
                if time.monotonic() - last_heartbeat >= 12:
                    yield ": keep-alive\n\n"
                    last_heartbeat = time.monotonic()
                await asyncio.sleep(.3)

        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return router
