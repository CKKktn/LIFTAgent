from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext

from fastapi import HTTPException

from .db import now, uid
from .models import provider_error


def text_content(message):
    content = message.content
    return content if isinstance(content, str) else "".join(item.get("text", "") for item in content if isinstance(item, dict))


class ChatService:
    """Session compatibility and the shared durable collaborative-review runner."""
    def __init__(self, db, models, retrieval):
        self.db, self.models, self.retrieval = db, models, retrieval
        self.locks = {}
        self.workflow = None

    def bind_workflow(self, service):
        self.workflow = service
        self.graph = service.graph

    def mutation_guard(self):
        return self.workflow._lock if self.workflow else nullcontext()

    def assert_idle(self, session_id):
        lock = self.locks.get(session_id)
        if lock and lock.locked():
            raise HTTPException(409, "该会话正在生成回复，请先停止或等待回复完成，再删除会话或助手")
        if self.workflow:
            self.workflow.assert_session_idle(session_id, deleting=True)

    def cleanup_jobs(self, identifiers):
        if self.workflow:
            self.workflow.forget_jobs(identifiers)

    def session(self, identifier, chat_id=None):
        row = self.db.one("SELECT * FROM sessions WHERE id=?", (identifier,))
        if not row or (chat_id and row["chat_id"] != chat_id):
            raise HTTPException(404, "会话不存在或不属于此助手")
        messages = json.loads(row["messages"])
        return {**row, "messages": messages, "create_time": row["created_at"], "update_time": row["updated_at"]}

    def create_session(self, chat_id, name):
        if not self.db.json_get("chats", chat_id):
            raise HTTPException(404, "助手不存在")
        identifier, timestamp = uid(), now()
        self.db.execute("INSERT INTO sessions(id,chat_id,name,created_at,updated_at) VALUES (?,?,?,?,?)", (identifier, chat_id, str(name or "新会话")[:200], timestamp, timestamp))
        return self.session(identifier)

    def validate_completion(self, body):
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages or len(messages) > 60:
            raise HTTPException(400, "messages 必须包含 1–60 条消息")
        if sum(len(str(message.get("content", ""))) for message in messages if isinstance(message, dict)) > 220000:
            raise HTTPException(413, "本次模型输入过长，请缩小材料范围")
        if any(not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "system"} or not isinstance(message.get("content"), str) for message in messages):
            raise HTTPException(400, "消息角色或内容格式无效")
        chat = self.db.json_get("chats", body.get("chat_id", "")) if body.get("chat_id") else None
        if body.get("chat_id") and not chat:
            raise HTTPException(404, "助手不存在")
        model_id = body.get("llm_id") or (chat.get("llm_id") if chat else "") or ""
        self.models.find(model_id, "chat")
        if chat and body.get("session_id"):
            self.session(body["session_id"], chat["id"])
            if self.locks.get(body["session_id"]) and self.locks[body["session_id"]].locked():
                raise HTTPException(409, "该会话正在生成回答，请先等待完成或停止")
        return chat, model_id

    async def completion(self, body, assistant, model_id, owner="legacy-local"):
        # API-console/legacy extraction calls without an assistant remain direct.
        if not assistant:
            answer = ""
            try:
                messages = [{"role": item["role"], "content": item["content"]} for item in body["messages"]]
                llm = self.models.chat(model_id, body.get("temperature", .1), body.get("max_tokens", 8192))
                async for chunk in llm.astream(messages):
                    answer += text_content(chunk)
                    yield {"code": 0, "data": {"answer": answer}}
                yield {"code": 0, "data": True}
            except asyncio.CancelledError:
                raise
            except Exception as error:
                yield {"code": 1, "message": provider_error(error)}
            return
        if not self.workflow:
            yield {"code": 1, "message": "协审工作流尚未就绪，请重新启动服务"}
            return
        question = next((item["content"] for item in reversed(body["messages"]) if item["role"] == "user"), "").strip()
        payload = {"chat_id": assistant["id"], "session_id": body.get("session_id"), "question": question,
            "model_id": model_id, "require_confirmation": body.get("require_confirmation", False)}
        if body.get("request_id"):
            payload["request_id"] = body["request_id"]
        try:
            job = await asyncio.to_thread(self.workflow.create, payload, owner)
            revision = -1
            while True:
                if job["revision"] != revision:
                    revision = job["revision"]
                    yield {"code": 0, "data": {"answer": job["answer"], "reference": job["reference"],
                        "session_id": job["session_id"], "workflow": job}}
                if job["status"] not in {"pending", "running", "stopping"}:
                    if job["status"] in {"failed", "stale"}:
                        yield {"code": 1, "message": job["message"]}
                    else:
                        # Stream completion is distinct from human confirmation.
                        yield {"code": 0, "data": True}
                    return
                await asyncio.sleep(.12)
                job = await asyncio.to_thread(self.workflow.get, job["id"], owner)
        except asyncio.CancelledError:
            # A disconnected observer never cancels the durable backend worker.
            raise
        except Exception as error:
            yield {"code": 1, "message": provider_error(error)}
