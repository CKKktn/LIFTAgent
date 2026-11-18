from __future__ import annotations

import json

from fastapi import HTTPException

from .db import dumps, now, uid


class RecordService:
    def __init__(self, db, knowledge, chat):
        self.db, self.knowledge, self.chat = db, knowledge, chat

    @staticmethod
    def public(row, detail=True):
        value = {**row, "contracts": json.loads(row["contracts"])}
        if detail:
            value["report"] = json.loads(row["report"])
        else:
            value.pop("report", None)
        return value

    def get(self, identifier):
        row = self.db.one("SELECT * FROM review_records WHERE id=?", (identifier,))
        if not row:
            raise HTTPException(404, "审查记录不存在")
        return self.public(row)

    def list(self, query="", status=""):
        rows = self.db.rows("SELECT * FROM review_records ORDER BY updated_at DESC")
        return [self.public(row, False) for row in rows if (not status or row["status"] == status) and (not query or query.casefold() in " ".join(str(row[key]) for key in ("title", "kb_name", "contracts", "notes")).casefold())]

    def save(self, body, identifier=None, *, _workflow=False):
        report = body.get("report")
        if not isinstance(report, dict) or not isinstance(report.get("requirements", []), list) or not isinstance(report.get("results", []), list):
            raise HTTPException(400, "审查报告结构无效")
        if len(dumps(report).encode()) > 15 * 1024 * 1024:
            raise HTTPException(413, "单条报告超过 15 MB，请缩小审查范围")
        title = str(report.get("title") or "合同审查")[:200]
        kb = str(report.get("kb") or "")
        if not identifier:
            self.knowledge.dataset(kb)
        status = str(body.get("status", "已完成" if report.get("complete") else "未开始"))
        if status not in {"已完成", "正在生成", "未开始"}:
            raise HTTPException(400, "任务状态无效")
        contracts = [{"id": document.get("id"), "name": document.get("name", "")} for document in report.get("baselineDocs", [])]
        timestamp = now()
        with self.db.transaction() as connection:
            if identifier:
                self._check_workflow_write(connection, identifier, _workflow)
                row = connection.execute("SELECT * FROM review_records WHERE id=?", (identifier,)).fetchone()
                if not row:
                    raise HTTPException(404, "审查记录不存在")
                if type(body.get("revision")) is not int or body["revision"] != row["revision"]:
                    raise HTTPException(409, "记录已被修改，请重新打开后再编辑")
                connection.execute("UPDATE review_records SET report=?,title=?,status=?,kb=?,kb_name=?,contracts=?,notes=?,revision=revision+1,updated_at=? WHERE id=?",
                    (dumps(report), title, status, kb, report.get("kbName", ""), dumps(contracts), str(body.get("notes", row["notes"]))[:20000], timestamp, identifier))
            else:
                identifier = uid()
                connection.execute("INSERT INTO review_records(id,report,title,status,kb,kb_name,contracts,notes,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (identifier, dumps(report), title, status, kb, report.get("kbName", ""), dumps(contracts), str(body.get("notes", ""))[:20000], timestamp, timestamp))
        return self.get(identifier)

    def delete(self, identifier, revision):
        with self.db.transaction() as connection:
            self._check_workflow_write(connection, identifier, False, deleting=True)
            row = connection.execute("SELECT revision FROM review_records WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise HTTPException(404, "审查记录不存在")
            if type(revision) is not int or revision != row["revision"]:
                raise HTTPException(409, "记录已被修改，请刷新后重试")
            connection.execute("DELETE FROM review_records WHERE id=?", (identifier,))

    @staticmethod
    def _check_workflow_write(connection, identifier, internal, *, deleting=False):
        if internal:
            return
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_jobs'").fetchone():
            return
        job = connection.execute("SELECT status FROM review_jobs WHERE record_id=?", (identifier,)).fetchone()
        allowed = {"completed", "stopped", "failed", "stale"} if deleting else {"completed"}
        if job and job["status"] not in allowed:
            raise HTTPException(409, "此记录由快速审查工作流管理，请在任务中确认、停止或继续，避免覆盖检查点。")

    def annotations(self, session):
        self.chat.session(session)
        return self.db.rows("SELECT message_index,state,note,revision,updated_at FROM annotations WHERE session_id=? ORDER BY message_index", (session,))

    def annotate(self, session_id, body, migration=False):
        session = self.chat.session(session_id)
        index = body.get("message_index")
        if type(index) is not int or not 0 <= index < len(session["messages"]) or session["messages"][index].get("role") != "assistant":
            raise HTTPException(400, "只能为实际存在的助手回复添加复核标注")
        with self.db.transaction() as connection:
            row = connection.execute("SELECT revision,state,note FROM annotations WHERE session_id=? AND message_index=?", (session_id, index)).fetchone()
            if migration and row:
                return
            state = body.get("state", row["state"] if row else "none")
            note = body.get("note", row["note"] if row else "")
            if state not in {"none", "reviewed", "important"} or not isinstance(note, str) or len(note) > 10000:
                raise HTTPException(400, "标注状态或备注格式无效")
            revision = row["revision"] if row else 0
            if not migration and (type(body.get("revision")) is not int or body["revision"] != revision):
                raise HTTPException(409, "标注已被其他窗口修改，请同步后重试")
            connection.execute("INSERT INTO annotations(session_id,message_index,state,note,revision,updated_at) VALUES (?,?,?,?,?,?) ON CONFLICT(session_id,message_index) DO UPDATE SET state=excluded.state,note=excluded.note,revision=excluded.revision,updated_at=excluded.updated_at",
                (session_id, index, state, note, revision + 1, now()))
        return next(item for item in self.annotations(session_id) if item["message_index"] == index)
