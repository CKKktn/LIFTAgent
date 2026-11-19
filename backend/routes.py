from __future__ import annotations

import asyncio
import json
import mimetypes
from pathlib import PurePosixPath

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .db import dumps, now, uid
from .drawing_evidence import DrawingReviewInput
from .models import provider_error
from .parsers import ALLOWED_EXTENSIONS, TEXT_EXTENSIONS
from .security import require_user


def ok(data=True, **kwargs):
    return {"code": 0, "data": data, **kwargs}


def page_values(page, page_size):
    return max(1, int(page)), max(1, min(200, int(page_size)))


def ids_from(body, field="ids"):
    value = body.get(field)
    if not isinstance(value, list) or not value or len(value) > 1000 or any(not isinstance(item, str) for item in value):
        raise HTTPException(400, f"{field} 必须包含 1–1000 个有效 ID")
    return list(dict.fromkeys(value))


def make_api(db, models, knowledge, retrieval, chat):
    router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_user)])

    @router.get("/system/healthz")
    def health():
        db.one("SELECT 1")
        return {"status": "ok", "db": "ok", "storage": "ok", "doc_engine": "local", "engine": "LangChain + LangGraph"}

    @router.get("/datasets")
    def list_datasets(page: int = 1, page_size: int = 100, name: str = "", id: str = ""):
        page, page_size = page_values(page, page_size)
        values = [knowledge.dataset(item["id"]) for item in db.json_list("datasets") if (not name or name.casefold() in item["name"].casefold()) and (not id or item["id"] == id)]
        return ok(values[(page - 1) * page_size:page * page_size], total_datasets=len(values))

    @router.post("/datasets")
    def create_dataset(body: dict = Body(...)):
        return ok(knowledge.save_dataset(body))

    @router.get("/datasets/{dataset_id}")
    def get_dataset(dataset_id: str):
        return ok(knowledge.dataset(dataset_id))

    @router.put("/datasets/{dataset_id}")
    def update_dataset(dataset_id: str, body: dict = Body(...)):
        return ok(knowledge.save_dataset(body, dataset_id))

    @router.delete("/datasets")
    def delete_datasets(body: dict = Body(...)):
        identifiers = ids_from(body)
        knowledge.remove_datasets(identifiers)
        return ok()

    @router.get("/datasets/{dataset_id}/documents")
    def list_documents(dataset_id: str, page: int = 1, page_size: int = 20, keywords: str = "", id: str = "", name: str = ""):
        knowledge.dataset(dataset_id)
        page, page_size = page_values(page, page_size)
        rows = db.rows("SELECT * FROM documents WHERE dataset_id=? ORDER BY created_at DESC, id DESC", (dataset_id,))
        rows = [row for row in rows if (not id or row["id"] == id) and (not keywords or keywords.casefold() in row["name"].casefold()) and (not name or row["name"] == name)]
        return ok({"docs": [knowledge.public_document(row) for row in rows[(page - 1) * page_size:page * page_size]], "total": len(rows)})

    @router.post("/datasets/{dataset_id}/documents")
    async def upload_documents(dataset_id: str, file: list[UploadFile] = File(...)):
        knowledge.dataset(dataset_id)
        if len(file) > 100:
            raise HTTPException(400, "单次最多上传 100 个文件")
        names = []
        for upload in file:
            name = str(upload.filename or "").replace("\\", "/")
            parsed = PurePosixPath(name)
            if not name or len(name) > 500 or parsed.is_absolute() or ".." in parsed.parts or ":" in name or "\x00" in name:
                raise HTTPException(400, "文件名或文件夹路径无效")
            if parsed.suffix.lower() not in ALLOWED_EXTENSIONS:
                raise HTTPException(400, "不支持文件类型：" + parsed.suffix)
            if upload.size is not None and upload.size > knowledge.settings.max_upload_mb * 1024 * 1024:
                raise HTTPException(413, "单文件超过上传上限")
            names.append(name)
        entries, paths = [], []
        try:
            for upload, name in zip(file, names):
                path = knowledge.settings.data_dir / "uploads" / (uid() + PurePosixPath(name).suffix.lower())
                paths.append(path)
                size = 0
                with path.open("xb") as destination:
                    while block := await upload.read(1024 * 1024):
                        size += len(block)
                        if size > knowledge.settings.max_upload_mb * 1024 * 1024:
                            raise HTTPException(413, "单文件超过上传上限")
                        destination.write(block)
                if not size:
                    raise HTTPException(400, "不能上传空文件")
                entries.append((name, path, size))
            output = knowledge.add_documents(dataset_id, entries)
        except BaseException:
            for path in paths:
                path.unlink(missing_ok=True)
            raise
        finally:
            for upload in file:
                await upload.close()
        return ok(output)

    @router.patch("/datasets/{dataset_id}/documents/{document_id}")
    def update_document(dataset_id: str, document_id: str, body: dict = Body(...)):
        return ok(knowledge.update_document(document_id, body, dataset_id))

    @router.delete("/datasets/{dataset_id}/documents")
    def delete_documents(dataset_id: str, body: dict = Body(...)):
        knowledge.remove_documents(ids_from(body), dataset_id)
        return ok()

    @router.post("/datasets/{dataset_id}/chunks")
    def parse_documents(dataset_id: str, body: dict = Body(...)):
        knowledge.dataset(dataset_id)
        knowledge.parse(ids_from(body, "document_ids"), dataset_id)
        return ok()

    @router.delete("/datasets/{dataset_id}/chunks")
    def stop_parsing(dataset_id: str, body: dict = Body(...)):
        knowledge.parse(ids_from(body, "document_ids"), dataset_id, cancel=True)
        return ok()

    @router.post("/documents/ingest")
    def ingest(body: dict = Body(...)):
        knowledge.parse(ids_from(body, "doc_ids"), cancel=str(body.get("run", "1")) == "2")
        return ok()

    @router.get("/datasets/{dataset_id}/documents/{document_id}/chunks")
    def list_chunks(dataset_id: str, document_id: str, page: int = 1, page_size: int = 10, keywords: str = "", id: str = ""):
        knowledge.document(document_id, dataset_id)
        page, page_size = page_values(page, page_size)
        rows = db.rows("SELECT * FROM chunks WHERE document_id=? ORDER BY ordinal,id", (document_id,))
        rows = [row for row in rows if (not keywords or keywords.casefold() in row["content"].casefold()) and (not id or row["id"] == id)]
        return ok({"chunks": [knowledge.chunk_view(row) for row in rows[(page - 1) * page_size:page * page_size]], "total": len(rows)})

    @router.get("/datasets/{dataset_id}/documents/{document_id}/drawing-evidence")
    def list_drawing_evidence(dataset_id: str, document_id: str, page: int = 1, page_size: int = 20):
        page, page_size = page_values(page, page_size)
        return ok(knowledge.drawing_records(dataset_id, document_id, page, page_size))

    @router.get("/datasets/{dataset_id}/documents/{document_id}/drawing-evidence/{evidence_id}")
    def get_drawing_evidence(dataset_id: str, document_id: str, evidence_id: str):
        return ok(knowledge.drawing_record(dataset_id, document_id, evidence_id))

    @router.patch("/datasets/{dataset_id}/documents/{document_id}/drawing-evidence/{evidence_id}")
    def review_drawing_evidence(dataset_id: str, document_id: str, evidence_id: str, body: DrawingReviewInput):
        try:
            return ok(knowledge.review_drawing(dataset_id, document_id, evidence_id, body))
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(503, provider_error(error)) from None

    @router.post("/datasets/{dataset_id}/documents/{document_id}/chunks")
    def add_chunk(dataset_id: str, document_id: str, body: dict = Body(...)):
        return ok(knowledge.save_chunk(dataset_id, document_id, body))

    @router.patch("/datasets/{dataset_id}/documents/{document_id}/chunks/{chunk_id}")
    def edit_chunk(dataset_id: str, document_id: str, chunk_id: str, body: dict = Body(...)):
        return ok(knowledge.save_chunk(dataset_id, document_id, body, chunk_id))

    @router.delete("/datasets/{dataset_id}/documents/{document_id}/chunks")
    def delete_chunks(dataset_id: str, document_id: str, body: dict = Body(...)):
        knowledge.delete_chunks(dataset_id, document_id, ids_from(body, "chunk_ids"))
        return ok()

    def original(document_id, dataset_id=None):
        row = knowledge.document(document_id, dataset_id)
        extension = row["extension"]
        kind = "text/plain; charset=utf-8" if extension in TEXT_EXTENSIONS else mimetypes.guess_type(row["name"])[0] or "application/octet-stream"
        return FileResponse(knowledge.file_path(row), media_type=kind, filename=PurePosixPath(row["name"]).name)

    @router.get("/datasets/{dataset_id}/documents/{document_id}")
    def download_scoped(dataset_id: str, document_id: str):
        return original(document_id, dataset_id)

    @router.get("/documents/{document_id}")
    def download(document_id: str):
        return original(document_id)

    @router.get("/documents/{document_id}/preview")
    def preview(document_id: str):
        row = knowledge.document(document_id)
        kind = "text/plain; charset=utf-8" if row["extension"] in TEXT_EXTENSIONS else mimetypes.guess_type(row["name"])[0] or "application/octet-stream"
        return FileResponse(knowledge.file_path(row), media_type=kind, headers={"Content-Disposition": "inline"})

    @router.post("/retrieval")
    def retrieve(body: dict = Body(...)):
        return ok(retrieval.search(body))

    @router.get("/models")
    def list_models():
        return ok(models.all())

    @router.get("/models/default")
    def get_defaults():
        return ok(models.defaults())

    @router.patch("/models/default")
    def update_defaults(body: dict = Body(...)):
        kind = body.get("model_type", "")
        if kind not in {"chat", "embedding", "rerank"}:
            raise HTTPException(400, "模型类型无效")
        models.set_default(kind, body.get("model_id", ""))
        return ok(models.defaults())

    @router.get("/chats")
    def list_chats(page: int = 1, page_size: int = 100, name: str = "", id: str = ""):
        page, page_size = page_values(page, page_size)
        rows = db.rows("""SELECT c.payload, EXISTS(SELECT 1 FROM sessions s WHERE s.chat_id=c.id) AS has_sessions
            FROM chats c ORDER BY c.rowid DESC""")
        values = []
        for row in rows:
            value = json.loads(row["payload"])
            # Imports created one assistant per knowledge base. Hide empty remnants while keeping conversation history accessible.
            if value.get("import_source") and not value.get("dataset_ids") and not row["has_sessions"]:
                continue
            if (not name or name.casefold() in value["name"].casefold()) and (not id or value["id"] == id):
                values.append(value)
        return ok(values[(page - 1) * page_size:page * page_size])

    def save_chat(body, identifier=None):
        previous = db.json_get("chats", identifier) if identifier else None
        if identifier and not previous:
            raise HTTPException(404, "审查助手不存在")
        name = str(body.get("name", previous["name"] if previous else "")).strip()
        if not name or len(name) > 200:
            raise HTTPException(400, "助手名称不能为空，且不得超过 200 字")
        dataset_ids = body.get("dataset_ids", previous.get("dataset_ids", []) if previous else [])
        if not isinstance(dataset_ids, list) or len(dataset_ids) > 50:
            raise HTTPException(400, "助手最多关联 50 个知识库")
        for dataset_id in dataset_ids:
            knowledge.dataset(dataset_id)
        value = {**(previous or {}), "id": identifier or uid(), "name": name, "dataset_ids": list(dict.fromkeys(dataset_ids)),
            "llm_id": body.get("llm_id", previous.get("llm_id", "") if previous else ""),
            "similarity_threshold": max(0, min(1, float(body.get("similarity_threshold", 0.15)))),
            "create_time": previous["create_time"] if previous else now(), "update_time": now()}
        if value["llm_id"]:
            value["llm_id"] = models.find(value["llm_id"], "chat")["id"]
        db.json_save("chats", value)
        return value

    @router.post("/chats")
    def create_chat(body: dict = Body(...)):
        return ok(save_chat(body))

    @router.get("/chats/{chat_id}")
    def get_chat(chat_id: str):
        value = db.json_get("chats", chat_id)
        if not value:
            raise HTTPException(404, "助手不存在")
        return ok(value)

    @router.patch("/chats/{chat_id}")
    @router.put("/chats/{chat_id}")
    def update_chat(chat_id: str, body: dict = Body(...)):
        return ok(save_chat(body, chat_id))

    @router.delete("/chats/{chat_id}")
    def delete_chat(chat_id: str):
        with chat.mutation_guard():
            for row in db.rows("SELECT id FROM sessions WHERE chat_id=?", (chat_id,)):
                chat.assert_idle(row["id"])
            jobs = [row["id"] for row in db.rows("SELECT id FROM collab_jobs WHERE chat_id=?", (chat_id,))]
            if not db.execute("DELETE FROM chats WHERE id=?", (chat_id,)):
                raise HTTPException(404, "助手不存在")
            chat.cleanup_jobs(jobs)
        return ok()

    @router.get("/chats/{chat_id}/sessions")
    def list_sessions(chat_id: str, page: int = 1, page_size: int = 20):
        if not db.json_get("chats", chat_id):
            raise HTTPException(404, "助手不存在")
        page, page_size = page_values(page, page_size)
        rows = db.rows("SELECT id,chat_id,name,revision,created_at AS create_time,updated_at AS update_time FROM sessions WHERE chat_id=? ORDER BY updated_at DESC LIMIT ? OFFSET ?", (chat_id, page_size, (page - 1) * page_size))
        return ok(rows)

    @router.post("/chats/{chat_id}/sessions")
    def create_session(chat_id: str, body: dict = Body(...)):
        return ok(chat.create_session(chat_id, body.get("name", "新会话")))

    @router.get("/chats/{chat_id}/sessions/{session_id}")
    def get_session(chat_id: str, session_id: str):
        return ok(chat.session(session_id, chat_id))

    @router.patch("/chats/{chat_id}/sessions/{session_id}")
    def rename_session(chat_id: str, session_id: str, body: dict = Body(...)):
        chat.session(session_id, chat_id)
        name = str(body.get("name", "")).strip()
        if not name or len(name) > 200:
            raise HTTPException(400, "会话名称无效")
        db.execute("UPDATE sessions SET name=?,revision=revision+1,updated_at=? WHERE id=?", (name, now(), session_id))
        return ok(chat.session(session_id))

    @router.delete("/chats/{chat_id}/sessions")
    def delete_sessions(chat_id: str, body: dict = Body(...)):
        identifiers = ids_from(body)
        with chat.mutation_guard():
            jobs = []
            for identifier in identifiers:
                chat.session(identifier, chat_id)
                chat.assert_idle(identifier)
                jobs.extend(row["id"] for row in db.rows("SELECT id FROM collab_jobs WHERE session_id=?", (identifier,)))
            with db.transaction() as connection:
                for identifier in identifiers:
                    connection.execute("DELETE FROM sessions WHERE id=? AND chat_id=?", (identifier, chat_id))
            chat.cleanup_jobs(jobs)
        return ok()

    @router.post("/chat/completions")
    async def completion(body: dict = Body(...), owner: str = Depends(require_user)):
        assistant, model_id = chat.validate_completion(body)
        events = chat.completion(body, assistant, model_id, owner)
        if body.get("stream", True):
            async def stream():
                async for event in events:
                    yield "data: " + dumps(event) + "\n\n"
            return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        result = {}
        async for event in events:
            if event.get("code"):
                raise HTTPException(502, event["message"])
            if isinstance(event.get("data"), dict):
                result = event["data"]
        return ok(result)

    return router
