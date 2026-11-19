from __future__ import annotations

from contextlib import asynccontextmanager
import importlib.metadata
import json
import logging
import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from .chat import ChatService
from .collab_jobs import CollabJobService, make_collab_jobs_router
from .config import PROJECT_ROOT, Settings
from .db import Database, dumps, now, uid
from .drawing_evidence import DRAWING_RECOGNITION_METHOD
from .knowledge import KnowledgeService
from .models import ModelRegistry, provider_error
from .records import RecordService
from .review_jobs import ReviewJobService, make_review_jobs_router
from .retrieval import RetrievalService
from .routes import make_api, ok
from .security import LoginLimiter, hash_password, require_user, verify_password


class ProxyCompatibilityMiddleware:
    """Route the original UI's proxy URLs internally; never forward arbitrary URLs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/proxy":
            path = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace")).get("path", [""])[0]
            parsed = urlsplit(path)
            if len(path) > 16000 or parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/api/v1/") or "\\" in path:
                await JSONResponse({"code": 1, "message": "只允许访问本系统 /api/v1/ 下的接口"}, 400)(scope, receive, send)
                return
            scope = dict(scope)
            scope["path"] = unquote(parsed.path)
            scope["raw_path"] = parsed.path.encode()
            scope["query_string"] = parsed.query.encode()
        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    secret = settings.initialize()
    db = Database(settings.data_dir / "workspace.sqlite3")
    models = ModelRegistry(db, secret)
    knowledge = KnowledgeService(db, models, settings)
    retrieval = RetrievalService(db, models, knowledge)
    chat = ChatService(db, models, retrieval)
    records = RecordService(db, knowledge, chat)
    review_jobs = ReviewJobService(db, models, knowledge, retrieval, records, settings)
    collab_jobs = CollabJobService(db, models, knowledge, retrieval, settings)
    chat.bind_workflow(collab_jobs)
    limiter = LoginLimiter()
    db.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    if not db.one("SELECT value FROM app_meta WHERE key='initialized'"):
        dataset = knowledge.save_dataset({"name": "合同资料", "description": "上传合同、技术协议和相关证明材料", "embedding_model": "local-features"})
        db.json_save("chats", {"id": uid(), "name": "合同审查助手", "dataset_ids": [dataset["id"]], "llm_id": "", "similarity_threshold": 0.15, "create_time": now(), "update_time": now()})
        db.execute("INSERT INTO app_meta(key,value) VALUES ('initialized','1')")
    if os.getenv("ADMIN_PASSWORD") and not db.one("SELECT id FROM users LIMIT 1"):
        password = os.environ["ADMIN_PASSWORD"]
        username = (os.getenv("ADMIN_USERNAME") or "admin").strip()
        if not 8 <= len(password) <= 128 or not username or len(username) > 60:
            raise ValueError("ADMIN_PASSWORD 需为 8–128 个字符，ADMIN_USERNAME 需为 1–60 个字符")
        db.execute("INSERT INTO users(id,username,password_hash,created_at) VALUES (?,?,?,?)", (uid(), username, hash_password(password), now()))

    @asynccontextmanager
    async def lifespan(app):
        yield
        collab_jobs.close()
        review_jobs.close()
        knowledge.executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="LIFT 合同审查 · LangChain / LangGraph 后端", version="0.5.0", lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db, app.state.models, app.state.knowledge = db, models, knowledge
    app.state.retrieval, app.state.chat, app.state.records = retrieval, chat, records
    app.state.review_jobs = review_jobs
    app.state.collab_jobs = collab_jobs
    app.state.settings = settings
    app.add_middleware(ProxyCompatibilityMiddleware)
    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="lift_local_session",
        max_age=settings.session_hours * 3600, same_site="strict", https_only=settings.secure_cookie)

    @app.middleware("http")
    async def guard(request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if request.headers.get("sec-fetch-site") == "cross-site" or (origin and origin != str(request.base_url).rstrip("/")):
                return JSONResponse({"code": 1, "message": "拒绝跨站写入请求"}, 403)
        length = request.headers.get("content-length")
        if length and (not length.isdecimal() or int(length) > max(64, settings.max_upload_mb * 4) * 1024 * 1024):
            return JSONResponse({"code": 1, "message": "请求体过大"}, 413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self' data: blob:; connect-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; worker-src 'self' blob:; frame-src 'self' blob:; object-src 'self' blob:; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        return JSONResponse({"code": error.status_code, "message": str(error.detail)}, error.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse({"code": 400, "message": "请求参数格式无效"}, 400)

    @app.exception_handler(ValueError)
    async def value_error(request, error):
        return JSONResponse({"code": 400, "message": str(error)[:500]}, 400)

    @app.exception_handler(Exception)
    async def internal_error(request, error):
        logging.getLogger("lift").error("%s %s failed: %s", request.method, request.url.path, type(error).__name__)
        return JSONResponse({"code": 500, "message": provider_error(error)}, 500)

    @app.get("/health")
    def public_health():
        return {"status": "ok"}

    @app.get("/auth/status")
    def auth_status():
        return {"initialized": bool(db.one("SELECT id FROM users LIMIT 1"))}

    @app.post("/auth/setup")
    def setup(request: Request, body: dict = Body(...)):
        if request.client and request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            raise HTTPException(403, "首次设置请从服务器本机访问，或通过环境变量预设管理员")
        username = str(body.get("username", "admin")).strip()
        password = body.get("password", "")
        if not username or len(username) > 60 or not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise HTTPException(400, "账号不能为空；密码需为 8–128 个字符")
        if db.one("SELECT id FROM users LIMIT 1"):
            raise HTTPException(409, "管理员已设置，请直接登录")
        password_hash = hash_password(password)
        identifier = uid()
        with db.transaction() as connection:
            if connection.execute("SELECT id FROM users LIMIT 1").fetchone():
                raise HTTPException(409, "管理员已设置，请直接登录")
            connection.execute("INSERT INTO users(id,username,password_hash,created_at) VALUES (?,?,?,?)", (identifier, username, password_hash, now()))
        request.session.clear()
        request.session["user_id"] = identifier
        return {"success": True}

    @app.post("/auth/login")
    def login(request: Request, body: dict = Body(...)):
        address = request.client.host if request.client else "local"
        limiter.check(address)
        password = body.get("password", "")
        if not isinstance(password, str) or len(password) > 128:
            raise HTTPException(400, "账号或密码格式无效")
        user = db.one("SELECT * FROM users WHERE username=?", (str(body.get("username", "")),))
        if not user or not verify_password(password, user["password_hash"]):
            limiter.failed(address)
            raise HTTPException(401, "账号或密码错误")
        limiter.clear(address)
        request.session.clear()
        request.session["user_id"] = user["id"]
        return {"success": True}

    @app.post("/auth/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", 303)

    @app.get("/login")
    def login_page(request: Request):
        if request.session.get("user_id"):
            return RedirectResponse("/", 303)
        return FileResponse(settings.frontend_dir / "login.html", media_type="text/html")

    @app.get("/")
    def index(request: Request):
        if not request.session.get("user_id"):
            return RedirectResponse("/login", 303)
        return FileResponse(settings.frontend_dir / "index.html", media_type="text/html")

    @app.get("/config", dependencies=[Depends(require_user)])
    def config():
        return {"configured": True, "base": "", "engine": "LangChain", "llm_configured": bool(models.default_id("chat")),
            "drawing_recognition_method": DRAWING_RECOGNITION_METHOD,
            "embedding_mode": "本地文本特征" if models.default_id("embedding") == "local-features" else "外部嵌入模型",
            "versions": {name: importlib.metadata.version(name) for name in ("langchain", "langchain-openai", "langgraph")}}

    @app.get("/endpoints.json", dependencies=[Depends(require_user)])
    def endpoint_directory():
        return [{"name": route.name.replace("_", " "), "method": method, "path": route.path}
            for route in app.routes if getattr(route, "path", "").startswith("/api/v1/") for method in sorted(route.methods or []) if method not in {"HEAD", "OPTIONS"}]

    @app.get("/settings/models", dependencies=[Depends(require_user)])
    def model_settings():
        return {"models": models.all(), "defaults": models.defaults(), "llm_configured": bool(models.default_id("chat"))}

    @app.post("/settings/models", dependencies=[Depends(require_user)])
    def save_model(body: dict = Body(...)):
        return models.save(body)

    @app.delete("/settings/models/{model_id}", dependencies=[Depends(require_user)])
    def delete_model(model_id: str):
        model = next((item for item in models.all() if item["id"] == model_id), None)
        if not model or model["read_only"]:
            raise HTTPException(400, "该模型不存在或不能删除")
        if any(item.get("embedding_model") == model_id or item.get("parser_config", {}).get("drawing_vision_model") == model_id
               for item in db.json_list("datasets")):
            raise HTTPException(409, "模型仍被知识库使用，请先调整知识库配置")
        if any(item.get("llm_id") == model_id for item in db.json_list("chats")):
            raise HTTPException(409, "模型仍被助手使用，请先调整助手配置")
        with db.transaction() as connection:
            connection.execute("DELETE FROM models WHERE id=?", (model_id,))
            connection.execute("DELETE FROM model_defaults WHERE model_id=?", (model_id,))
        return {"success": True}

    @app.post("/settings/models/{model_id}/test", dependencies=[Depends(require_user)])
    async def test_model(model_id: str):
        model = next((item for item in models.all() if item["id"] == model_id), None)
        if not model:
            raise HTTPException(404, "模型不存在")
        try:
            if model["model_type"] == "chat":
                response = await models.chat(model_id, max_tokens=128).ainvoke("请仅回复 OK")
                return {"success": True, "message": "连接成功", "sample": str(response.content)[:100]}
            if model["model_type"] == "embedding":
                import asyncio
                import math
                _, embedding = models.embedding(model_id)
                vector = await asyncio.to_thread(embedding.embed_query, "连接测试")
                if not vector or not all(math.isfinite(float(value)) for value in vector):
                    raise HTTPException(502, "嵌入模型未返回有效向量，请检查接口用途与模型名称")
                return {"success": True, "message": f"连接成功，向量维度 {len(vector)}"}
            if model_id == "local-rerank":
                return {"success": True, "message": "内置混合排序在本机运行，无需连接外部服务"}
            import asyncio
            ranked = await asyncio.to_thread(retrieval.rerank, "连接测试：设备续航", ["合成测试文字：设备续航10小时。", "合成测试文字：文档归档说明。"], model_id)
            return {"success": True, "message": f"连接成功，重排接口返回 {len(ranked)} 条有效排序结果"}
        except Exception as error:
            raise HTTPException(502, provider_error(error)) from None

    @app.get("/review-records", dependencies=[Depends(require_user)])
    def list_records(q: str = "", status: str = ""):
        return records.list(q, status)

    @app.post("/review-records", dependencies=[Depends(require_user)])
    def create_record(body: dict = Body(...)):
        return records.save(body)

    @app.get("/review-records/{record_id}", dependencies=[Depends(require_user)])
    def get_record(record_id: str):
        return records.get(record_id)

    @app.put("/review-records/{record_id}", dependencies=[Depends(require_user)])
    def update_record(record_id: str, body: dict = Body(...)):
        return records.save(body, record_id)

    @app.delete("/review-records/{record_id}", dependencies=[Depends(require_user)])
    def delete_record(record_id: str, body: dict = Body(...)):
        records.delete(record_id, body.get("revision"))
        review_jobs.forget_completed_record(record_id)
        return {"success": True}

    @app.get("/conversation-annotations", dependencies=[Depends(require_user)])
    def list_annotations(session: str):
        return records.annotations(session)

    @app.put("/conversation-annotations", dependencies=[Depends(require_user)])
    def save_annotation(session: str, body: dict = Body(...)):
        return records.annotate(session, body)

    @app.post("/conversation-annotations", dependencies=[Depends(require_user)])
    def migrate_annotations(session: str, body: dict = Body(...)):
        values = body.get("rows")
        if not isinstance(values, list) or len(values) > 1000:
            raise HTTPException(400, "标注迁移格式无效")
        for value in values:
            records.annotate(session, value, migration=True)
        return records.annotations(session)

    @app.get("/extract-text", dependencies=[Depends(require_user)])
    def extract_text(document_id: str, name: str = ""):
        return knowledge.extracted_text(document_id)

    @app.get("/preview-office", dependencies=[Depends(require_user)])
    def preview_office(document_id: str, name: str = ""):
        return FileResponse(knowledge.preview(document_id), media_type="application/pdf", headers={"X-Preview-Mode": "reflowed-text"})

    app.include_router(make_api(db, models, knowledge, retrieval, chat))
    app.include_router(make_review_jobs_router(review_jobs))
    app.include_router(make_collab_jobs_router(collab_jobs))

    public_files = {"collab-jobs.js", "collab-jobs.css", "review-jobs.js", "report-export.js", "report-workspace.js", "report-workspace.css", "app.js", "api.js", "ui.js", "viewer.js", "review.js", "retrieval-review.js", "three-stage-review.js",
        "upload-filter.js", "backgrounds.js", "style.css", "backgrounds.css", "model-settings.js", "model-settings.css", "login.js", "review-state.js",
        "drawing-evidence.js", "drawing-evidence.css"}

    @app.get("/assets/{filename:path}")
    @app.get("/vendor/{filename:path}")
    def static_tree(request: Request, filename: str):
        top = "assets" if request.url.path.startswith("/assets/") else "vendor"
        root = (settings.frontend_dir / top).resolve()
        path = (root / filename).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404, "资源不存在")
        return FileResponse(path)

    @app.get("/{filename}")
    def static_file(filename: str):
        if filename not in public_files or not (settings.frontend_dir / filename).is_file():
            raise HTTPException(404, "页面不存在")
        return FileResponse(settings.frontend_dir / filename)

    return app


def serves_this_copy(host: str, port: int) -> bool:
    from http.client import HTTPConnection, HTTPException
    from secrets import token_bytes
    from tempfile import TemporaryDirectory

    # The running server can read assets from its own frontend directory.
    # A temporary file lets us distinguish two installations of the same build.
    connection = HTTPConnection(host, port, timeout=2)
    try:
        with TemporaryDirectory(prefix="launch-", dir=PROJECT_ROOT / "frontend" / "assets") as directory:
            probe = Path(directory) / "instance.txt"
            marker = token_bytes(16)
            probe.write_bytes(marker)
            connection.request("GET", f"/assets/{probe.parent.name}/{probe.name}")
            with connection.getresponse() as response:
                return response.status == 200 and response.read(len(marker) + 1) == marker
    except (OSError, HTTPException):
        return False
    finally:
        connection.close()


def serve() -> int:
    import errno
    import socket
    import webbrowser

    import uvicorn
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    host = os.getenv("APP_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("APP_PORT", "8000"))
        if not 0 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise RuntimeError("APP_PORT must be a number between 0 and 65535.") from None
    browser_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    url_host = f"[{browser_host}]" if ":" in browser_host else browser_host

    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as listener:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((host, port))
        except OSError as error:
            address_busy = error.errno == errno.EADDRINUSE or getattr(error, "winerror", None) in (10013, 10048)
            if address_busy and serves_this_copy(browser_host, port):
                url = f"http://{url_host}:{port}/"
                print(f"The system is already running. Open {url}", flush=True)
                try:
                    if webbrowser.open(url):
                        return 0
                except (OSError, webbrowser.Error):
                    pass
                print("Could not open the default browser. Copy the address above into your browser.", flush=True)
                return 1
            raise RuntimeError(
                f"Port {port} is already in use or unavailable on {host}. "
                "Check which program is using it before starting the system."
            ) from error

        port = listener.getsockname()[1]
        print(f"Open http://{url_host}:{port}/ once the system starts. Stop with Ctrl+C.", flush=True)
        config = uvicorn.Config(create_app, factory=True, host=host, port=port)
        server = uvicorn.Server(config)
        # Hold the same socket through startup so concurrent launches cannot both initialize the workspace.
        server.run(sockets=[listener])
        return 0 if server.started else 1


if __name__ == "__main__":
    try:
        raise SystemExit(serve())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except (OSError, RuntimeError) as error:
        raise SystemExit(f"Startup failed: {error}") from None
