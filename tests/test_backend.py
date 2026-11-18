from __future__ import annotations

from io import BytesIO
import hashlib
import json
from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from backend.config import Settings
from backend.main import create_app
from fake_model_provider import FakeProvider


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    for key in ("ADMIN_PASSWORD", "LLM_MODEL", "LLM_BASE_URL", "EMBEDDING_MODEL", "EMBEDDING_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    # Protocol fixtures run on loopback, independently of the user's proxy.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app) as client:
        response = client.post("/auth/setup", json={"username": "tester", "password": "test-only-password"})
        assert response.status_code == 200
        yield app, client


def data(response):
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["code"] == 0, value
    return value["data"]


def first_dataset(client):
    return data(client.get("/api/v1/datasets"))[0]["id"]


def upload(client, dataset, name, content):
    return data(client.post(f"/api/v1/datasets/{dataset}/documents", files={"file": (name, content, "application/octet-stream")}))[0]


def parse(client, dataset, documents):
    data(client.post(f"/api/v1/datasets/{dataset}/chunks", json={"document_ids": [document["id"] for document in documents]}))
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        rows = data(client.get(f"/api/v1/datasets/{dataset}/documents", params={"page_size": 100}))["docs"]
        selected = [row for row in rows if row["id"] in {document["id"] for document in documents}]
        if all(row["run"] != "RUNNING" for row in selected):
            assert all(row["run"] == "DONE" for row in selected), selected
            return selected
        time.sleep(0.03)
    pytest.fail("Parsing did not finish")


def test_authentication_persists_and_writes_reject_cross_site(workspace):
    app, client = workspace
    assert client.get("/auth/status").json()["initialized"]
    assert client.post("/auth/setup", json={"username": "new", "password": "another-password"}).status_code == 409
    assert client.post("/api/v1/datasets", json={"name": "bad"}, headers={"Origin": "https://evil.invalid"}).status_code == 403
    assert client.get("/proxy", params={"path": "https://evil.invalid/api/v1/datasets"}).status_code == 400
    assert client.get("/proxy", params={"path": "/api/v1/datasets?page=1&page_size=100"}).status_code == 200
    client.post("/auth/logout")
    assert client.get("/api/v1/datasets").status_code == 401
    assert client.post("/auth/login", json={"username": "tester", "password": "wrong"}).status_code == 401
    assert client.post("/auth/login", json={"username": "tester", "password": "test-only-password"}).status_code == 200
    assert client.get("/capture/site/config.json").status_code == 404
    script = client.get("/review-state.js")
    assert script.status_code == 200 and "refreshReviewMaterials" in script.text


def test_environment_admin_defaults_to_admin_when_username_is_blank(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "")
    monkeypatch.setenv("ADMIN_PASSWORD", "environment-only-test")
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        assert client.get("/auth/status").json()["initialized"]
        assert client.post("/auth/login", json={"username": "admin", "password": "environment-only-test"}).status_code == 200
        assert client.post("/auth/setup", json={"username": "tester", "password": "test-only-password"}).status_code == 409


def test_background_assets_are_available_before_login_and_in_source_package(workspace):
    app, client = workspace
    client.post("/auth/logout")
    manifest_response = client.get("/vendor/vanta/asset-manifest.json")
    assert manifest_response.status_code == 200
    manifest = manifest_response.json()
    assert manifest["packages"]["three"]["version"] == "0.134.0"
    assert manifest["packages"]["vanta"]["version"] == "0.5.24"
    expected_scripts = {"three.min.js", *("vanta." + effect + ".min.js" for effect in
        ("fog", "net", "waves", "birds", "globe", "halo", "rings"))}
    assert expected_scripts == {name for name in manifest["files"] if name.endswith(".js")}
    for name in expected_scripts:
        response = client.get("/vendor/vanta/" + name)
        assert response.status_code == 200, name
        assert "javascript" in response.headers["content-type"]
        assert hashlib.sha256(response.content).hexdigest() == manifest["files"][name]["sha256"]
        assert len(response.content) == manifest["files"][name]["bytes"]
    from tools.package_app import ROOT, application_files
    packaged = {path.relative_to(ROOT).as_posix() for path in application_files()}
    assert {"frontend/vendor/vanta/" + name for name in manifest["files"]}.issubset(packaged)
    assert "frontend/vendor/vanta/asset-manifest.json" in packaged


def test_local_workspace_works_without_any_external_http_service(workspace, monkeypatch):
    app, client = workspace

    def no_network(*args, **kwargs):
        pytest.fail("Unconfigured local workspace unexpectedly called an external HTTP service")

    async def no_async_network(*args, **kwargs):
        no_network()

    # TestClient uses an in-process ASGI transport. Block the real transports
    # used by httpx and provider SDKs while exercising the local application.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_async_network)
    config = client.get("/config").json()
    assert config["engine"] == "LangChain" and config["base"] == ""
    assert not config["llm_configured"]
    dataset = first_dataset(client)
    document = upload(client, dataset, "独立运行样例.txt", "本机演示设备的连续续航时间为10小时。".encode())
    parse(client, dataset, [document])
    result = data(client.post("/api/v1/retrieval", json={"question": "设备续航时间", "dataset_ids": [dataset], "similarity_threshold": 0.1}))
    assert result["chunks"] and result["chunks"][0]["document_id"] == document["id"]
    assert "10小时" in result["chunks"][0]["content"]
    assert "modelConnectionForm" in client.get("/").text
    not_ready = client.post("/api/v1/chat/completions", json={"messages": [{"role": "user", "content": "核对续航时间"}]})
    assert not_ready.status_code == 503
    assert "配置" in not_ready.json()["message"]


def test_real_document_index_retrieves_evidence_and_excludes_baseline(workspace):
    app, client = workspace
    dataset = first_dataset(client)
    baseline = upload(client, dataset, "技术协议.txt", "设备续航时间不得低于8小时。应提交设备测试报告。".encode())
    evidence = upload(client, dataset, "项目方案.txt", "设备采用双电池，续航时间为10小时。工作温度为零下10至50摄氏度。".encode())
    parse(client, dataset, [baseline, evidence])
    result = data(client.post("/api/v1/retrieval", json={"question": "设备续航时间", "dataset_ids": [dataset], "exclude_document_ids": [baseline["id"]], "similarity_threshold": 0.1}))
    assert result["chunks"]
    assert {row["document_id"] for row in result["chunks"]} == {evidence["id"]}
    assert "10小时" in result["chunks"][0]["content"]
    data(client.patch(f"/api/v1/datasets/{dataset}/documents/{evidence['id']}", json={"enabled": 0}))
    assert not data(client.post("/api/v1/retrieval", json={"question": "设备续航时间", "dataset_ids": [dataset], "exclude_document_ids": [baseline["id"]], "similarity_threshold": 0}))["chunks"]
    with TestClient(create_app(app.state.settings)) as second:
        second.post("/auth/login", json={"username": "tester", "password": "test-only-password"})
        rows = data(second.get(f"/api/v1/datasets/{dataset}/documents"))["docs"]
        assert len(rows) == 2 and all(row["run"] == "DONE" for row in rows)


def test_pdf_word_excel_and_powerpoint_parsing_and_preview(workspace):
    _, client = workspace
    dataset = first_dataset(client)
    from reportlab.pdfgen import canvas
    output = BytesIO()
    pdf = canvas.Canvas(output)
    pdf.drawString(40, 750, "Battery endurance shall be at least 8 hours")
    pdf.save()
    pdf_bytes = output.getvalue()
    from docx import Document
    output = BytesIO()
    word = Document()
    word.add_paragraph("续航应达到8小时")
    word.add_table(rows=1, cols=2).rows[0].cells[0].text = "设备参数"
    word.save(output)
    word_bytes = output.getvalue()
    from openpyxl import Workbook
    output = BytesIO()
    excel = Workbook()
    excel.active.append(["参数", "数值"])
    excel.active.append(["续航", "10小时"])
    excel.save(output)
    excel.close()
    from pptx import Presentation
    from pptx.util import Inches
    powerpoint = Presentation()
    slide = powerpoint.slides.add_slide(powerpoint.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(2)).text = "系统具备远程监控功能"
    ppt_output = BytesIO()
    powerpoint.save(ppt_output)
    docs = [upload(client, dataset, "contract.pdf", pdf_bytes), upload(client, dataset, "条件.docx", word_bytes), upload(client, dataset, "指标.xlsx", output.getvalue()), upload(client, dataset, "功能.pptx", ppt_output.getvalue())]
    parse(client, dataset, docs)
    for document in docs:
        chunks = data(client.get(f"/api/v1/datasets/{dataset}/documents/{document['id']}/chunks"))["chunks"]
        assert chunks
        assert client.get("/extract-text", params={"document_id": document["id"]}).json()["text"]
    assert client.get(f"/api/v1/documents/{docs[0]['id']}").content == pdf_bytes
    preview = client.get("/preview-office", params={"document_id": docs[1]["id"]})
    assert preview.status_code == 200 and preview.content.startswith(b"%PDF-")
    assert client.post(f"/api/v1/datasets/{dataset}/documents", files={"file": ("../../escape.txt", b"bad")}).status_code == 400
    assert client.post(f"/api/v1/datasets/{dataset}/documents", files={"file": ("script.exe", b"bad")}).status_code == 400


def test_chunk_edit_updates_search_and_membership_is_enforced(workspace):
    _, client = workspace
    dataset = first_dataset(client)
    other = data(client.post("/api/v1/datasets", json={"name": "其他库"}))["id"]
    document = upload(client, dataset, "材料.txt", "测试材料".encode())
    parse(client, dataset, [document])
    chunk = data(client.get(f"/api/v1/datasets/{dataset}/documents/{document['id']}/chunks"))["chunks"][0]
    path = f"/api/v1/datasets/{dataset}/documents/{document['id']}/chunks"
    data(client.patch(path + "/" + chunk["id"], json={"content": "接口应支持CAN总线并兼容Ethernet通信。"}))
    result = data(client.post("/api/v1/retrieval", json={"dataset_ids": [dataset], "question": "CAN Ethernet接口", "similarity_threshold": 0.1}))
    assert "Ethernet" in result["chunks"][0]["content"]
    assert client.get(f"/api/v1/datasets/{other}/documents/{document['id']}/chunks").status_code == 404
    data(client.post(path, json={"content": "人工补充的有效记录"}))
    assert data(client.get(path))["total"] == 2
    data(client.request("DELETE", path, json={"chunk_ids": [chunk["id"]]}))
    assert data(client.get(path))["total"] == 1


def test_cancelled_parse_cannot_commit_late_chunks(workspace, monkeypatch):
    app, client = workspace
    dataset = first_dataset(client)
    document = upload(client, dataset, "long.txt", "设备技术说明".encode())
    import backend.knowledge as module
    original = module.extract
    entered, release = threading.Event(), threading.Event()
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(4)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "extract", delayed)
    data(client.post(f"/api/v1/datasets/{dataset}/chunks", json={"document_ids": [document["id"]]}))
    assert entered.wait(2)
    data(client.request("DELETE", f"/api/v1/datasets/{dataset}/chunks", json={"document_ids": [document["id"]]}))
    release.set()
    app.state.knowledge.executor.shutdown(wait=True)
    assert app.state.db.one("SELECT run FROM documents WHERE id=?", (document["id"],))["run"] == "CANCEL"
    assert app.state.db.one("SELECT COUNT(*) AS n FROM chunks WHERE document_id=?", (document["id"],))["n"] == 0


def test_review_records_use_optimistic_revision_checks(workspace):
    _, client = workspace
    dataset = first_dataset(client)
    report = {"title": "审查任务", "kb": dataset, "kbName": "合同资料", "requirements": [], "results": [], "baselineDocs": [], "complete": False}
    created = client.post("/review-records", json={"report": report}).json()
    path = "/review-records/" + created["id"]
    changed = client.put(path, json={"report": report, "revision": created["revision"], "notes": "人工复核"})
    assert changed.status_code == 200 and changed.json()["revision"] == 2
    assert client.put(path, json={"report": report, "revision": 1}).status_code == 409
    assert client.request("DELETE", path, json={"revision": 1}).status_code == 409
    assert client.get("/review-records", params={"q": "审查任务"}).json()[0]["notes"] == "人工复核"
    assert client.request("DELETE", path, json={"revision": 2}).status_code == 200
    assert client.get(path).status_code == 404


def test_unconfigured_model_is_an_explicit_error_not_a_fake_answer(workspace):
    _, client = workspace
    response = client.post("/api/v1/chat/completions", json={"messages": [{"role": "user", "content": "审查这个合同"}], "stream": True})
    assert response.status_code == 503
    assert "模型设置" in response.json()["message"]


def test_model_addresses_reject_placeholders_and_invalid_ports(workspace):
    app, client = workspace
    for address in ("https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "https://%7BWorkspaceId%7D.example.com/v1", "https://example.com/<workspace>/v1"):
        response = client.post("/settings/models", json={"name": "invalid-address", "base_url": address})
        assert response.status_code == 400 and "占位符" in response.json()["message"]
    for address in ("https://example.com:wrong/v1", "https://example.com:99999/v1", "https://example.com/has space"):
        assert client.post("/settings/models", json={"name": "invalid-address", "base_url": address}).status_code == 400
    assert not app.state.db.rows("SELECT id FROM models")
    # Legacy saved templates are still listed for editing, but cannot be called.
    app.state.db.execute("INSERT INTO models(id,payload,secret) VALUES (?,?,?)", ("old-template",
        json.dumps({"id": "old-template", "name": "old-template", "model_type": "chat", "instance_name": "test", "provider_name": "OpenAI-Compatible",
            "base_url": "https://{WorkspaceId}.example.com/v1"}), ""))
    assert client.get("/settings/models").status_code == 200
    tested = client.post("/settings/models/old-template/test")
    assert tested.status_code == 502 and "占位符" in tested.json()["message"]
    app.state.db.execute("DELETE FROM models WHERE id='old-template'")
    for address in ("https://api.deepseek.com", "http://127.0.0.1:11434/v1", "http://[::1]:11434/v1"):
        response = client.post("/settings/models", json={"name": "valid-address", "base_url": address})
        assert response.status_code == 200


def test_connection_errors_explain_network_without_exposing_provider_details():
    from backend.models import provider_error
    error = type("OpenAIConnectionError", (Exception,), {})("secret-should-not-appear")
    message = provider_error(error)
    assert "无法连接模型服务" in message and "代理" in message
    assert "secret-should-not-appear" not in message


def test_openai_compatible_stream_rag_secret_storage_and_annotations(workspace):
    app, client = workspace
    dataset = first_dataset(client)
    document = upload(client, dataset, "项目方案.txt", "设备采用双电池，设备续航时间为10小时。".encode())
    parse(client, dataset, [document])
    with FakeProvider() as provider:
        model = client.post("/settings/models", json={"name": "fixture-chat", "base_url": provider.base_url, "model_type": "chat", "api_key": "fixture-secret", "set_default": True}).json()
        assert "api_key" not in model and model["api_key_set"]
        assert "fixture-secret" not in client.get("/settings/models").text
        assert "fixture-secret" not in app.state.db.one("SELECT secret FROM models WHERE id=?", (model["id"],))["secret"]
        edited = client.post("/settings/models", json={**model, "api_key": "", "set_default": True}).json()
        assert edited["api_key_set"]
        assert app.state.models.find(model["id"], "chat", private=True)["api_key"] == "fixture-secret"
        assert client.post(f"/settings/models/{model['id']}/test").status_code == 200
        assistant = data(client.get("/api/v1/chats"))[0]
        session = data(client.post(f"/api/v1/chats/{assistant['id']}/sessions", json={"name": "续航核查"}))
        response = client.post("/api/v1/chat/completions", json={"chat_id": assistant["id"], "session_id": session["id"], "messages": [{"role": "user", "content": "设备续航时间是多少？"}], "stream": True})
        assert response.status_code == 200 and "text/event-stream" in response.headers["content-type"]
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert all(event["code"] == 0 for event in events)
        assert events[-1]["data"] is True
        replies = [event["data"] for event in events if isinstance(event["data"], dict)]
        assert "10小时" in replies[-1]["answer"]
        assert replies[-1]["reference"]["chunks"][0]["document_id"] == document["id"]
        saved = data(client.get(f"/api/v1/chats/{assistant['id']}/sessions/{session['id']}"))
        assert len(saved["messages"]) == 2
        assert saved["messages"][1]["replyState"] == "complete"
        mark_url = "/conversation-annotations?session=" + session["id"]
        mark = client.put(mark_url, json={"message_index": 1, "state": "important", "note": "已核对原文", "revision": 0})
        assert mark.status_code == 200 and mark.json()["revision"] == 1
        assert client.put(mark_url, json={"message_index": 1, "state": "reviewed", "note": "old", "revision": 0}).status_code == 409
        updated_note = client.put(mark_url, json={"message_index": 1, "note": "已核对原文并保留重要标记", "revision": 1})
        assert updated_note.json()["state"] == "important" and updated_note.json()["revision"] == 2
        updated_state = client.put(mark_url, json={"message_index": 1, "state": "reviewed", "revision": 2})
        assert updated_state.json()["note"] == "已核对原文并保留重要标记"
        assert client.get(mark_url).json()[0]["note"] == "已核对原文并保留重要标记"
        assert client.put(mark_url, json={"message_index": 0, "state": "reviewed", "note": "wrong role", "revision": 0}).status_code == 400


def test_remote_embedding_protocol_and_vector_persistence(workspace):
    app, client = workspace
    with FakeProvider() as provider:
        model = client.post("/settings/models", json={"name": "fixture-embedding", "model_type": "embedding", "base_url": provider.base_url}).json()
        dataset = data(client.post("/api/v1/datasets", json={"name": "远程向量测试", "embedding_model": model["id"]}))["id"]
        document = upload(client, dataset, "参数.txt", "设备续航时间为10小时。".encode())
        parse(client, dataset, [document])
        result = data(client.post("/api/v1/retrieval", json={"dataset_ids": [dataset], "question": "设备续航时间", "similarity_threshold": 0.1}))
        assert result["chunks"] and result["chunks"][0]["document_id"] == document["id"]
        changed = client.post("/settings/models", json={"id": model["id"], "name": "fixture-embedding-v2", "model_type": "embedding", "base_url": provider.base_url})
        assert changed.status_code == 200 and changed.json()["requires_reparse"]
        pending = data(client.get(f"/api/v1/datasets/{dataset}/documents"))["docs"][0]
        assert pending["run"] == "UNSTART" and "重新解析" in pending["progress_msg"]
        assert not data(client.post("/api/v1/retrieval", json={"dataset_ids": [dataset], "question": "设备续航时间", "similarity_threshold": 0}))["chunks"]
        assert client.post("/settings/models", json={"id": model["id"], "name": "changed-type", "model_type": "chat", "base_url": provider.base_url}).status_code == 400
        parse(client, dataset, [document])
        assert data(client.post("/api/v1/retrieval", json={"dataset_ids": [dataset], "question": "设备续航时间", "similarity_threshold": 0}))["chunks"]


def test_dataset_deletion_validates_whole_batch_and_cascades(workspace):
    app, client = workspace
    dataset = first_dataset(client)
    document = upload(client, dataset, "删除测试.txt", "这是一份合成的测试资料。".encode())
    parse(client, dataset, [document])
    path = app.state.knowledge.file_path(app.state.knowledge.document(document["id"]))
    preview = app.state.knowledge.preview(document["id"])
    failed = client.request("DELETE", "/api/v1/datasets", json={"ids": [dataset, "missing-dataset"]})
    assert failed.status_code == 404
    assert path.is_file() and preview.is_file()
    assert data(client.get(f"/api/v1/datasets/{dataset}/documents"))["total"] == 1
    data(client.request("DELETE", "/api/v1/datasets", json={"ids": [dataset]}))
    assert not path.exists() and not preview.exists()
    assert not app.state.db.rows("SELECT id FROM documents") and not app.state.db.rows("SELECT id FROM chunks")
    assert data(client.get("/api/v1/chats"))[0]["dataset_ids"] == []


def test_deleted_knowledge_base_hides_empty_imported_assistants_on_refresh(workspace):
    app, client = workspace
    removed = first_dataset(client)
    retained = data(client.post("/api/v1/datasets", json={"name": "其他资料"}))["id"]
    assistants = {}
    for name, links in {"本次删除": [removed], "过去已删除": [], "有历史": [removed], "仍有关联": [removed, retained]}.items():
        assistant = data(client.post("/api/v1/chats", json={"name": name, "dataset_ids": links}))
        app.state.db.json_save("chats", {**assistant, "import_source": {"url": "http://synthetic.invalid/", "dataset_id": name}})
        assistants[name] = assistant["id"]
    manual = data(client.post("/api/v1/chats", json={"name": "手工助手"}))["id"]
    history = assistants["有历史"]
    session = data(client.post(f"/api/v1/chats/{history}/sessions", json={"name": "原有会话"}))
    assert assistants["本次删除"] in {row["id"] for row in data(client.get("/api/v1/chats"))}

    data(client.request("DELETE", "/api/v1/datasets", json={"ids": [removed]}))

    visible = {row["id"]: row for row in data(client.get("/api/v1/chats"))}
    assert assistants["本次删除"] not in visible and assistants["过去已删除"] not in visible
    assert visible[assistants["仍有关联"]]["dataset_ids"] == [retained]
    assert visible[history]["dataset_ids"] == [] and manual in visible
    assert data(client.get(f"/api/v1/chats/{history}/sessions"))[0]["id"] == session["id"]


def cached_image(workspace):
    app, client = workspace
    dataset = first_dataset(client)
    original = app.state.settings.data_dir / "uploads" / "imported-test.png"
    from PIL import Image
    Image.new("RGB", (5, 5), "white").save(original)
    document = app.state.knowledge.add_document(dataset, "设备参数.png", original, original.stat().st_size)
    cache = app.state.settings.data_dir / "imported-text" / (document["id"] + ".json")
    cache.parent.mkdir(exist_ok=True)
    cache.write_text(json.dumps([{"page_content": "设备连续续航时间为14小时。", "metadata": {"source_chunk_id": "old-chunk-1"}}], ensure_ascii=False), encoding="utf-8")
    app.state.db.execute("UPDATE documents SET metadata=? WHERE id=?", (json.dumps({"imported_text_cache": True,
        "imported_text_sha256": hashlib.sha256(cache.read_bytes()).hexdigest(), "text_method": "原站已有解析文字（迁移）"}), document["id"]))
    return app, client, dataset, document, cache, original


def test_migrated_image_uses_existing_ocr_text_on_parse_and_preview(workspace):
    app, client, dataset, document, cache, original = cached_image(workspace)
    parse(client, dataset, [document])
    result = data(client.post("/api/v1/retrieval", json={"dataset_ids": [dataset], "question": "设备续航时间", "similarity_threshold": 0.1}))
    assert "14小时" in result["chunks"][0]["content"]
    assert result["chunks"][0]["document_name"] == "设备参数.png"
    assert "14小时" in client.get("/extract-text", params={"document_id": document["id"]}).json()["text"]
    assert client.get(f"/api/v1/documents/{document['id']}/preview").content == original.read_bytes()
    data(client.put(f"/api/v1/datasets/{dataset}", json={"parser_config": {"chunk_size": 200, "chunk_overlap": 20}}))
    parse(client, dataset, [document])
    assert app.state.knowledge.document(document["id"])["run"] == "DONE"
    data(client.request("DELETE", f"/api/v1/datasets/{dataset}/documents", json={"ids": [document["id"]]}))
    assert not cache.exists() and not original.exists()


def test_migrated_text_cache_corruption_fails_without_replacing_chunks(workspace):
    app, client, dataset, document, cache, original = cached_image(workspace)
    parse(client, dataset, [document])
    before = app.state.db.rows("SELECT id,content FROM chunks WHERE document_id=?", (document["id"],))
    cache.write_text("[]", encoding="utf-8")
    row = app.state.knowledge.document(document["id"])
    app.state.db.execute("UPDATE documents SET run='RUNNING',generation=generation+1 WHERE id=?", (document["id"],))
    app.state.knowledge._parse_one(document["id"], row["generation"] + 1)
    failed = app.state.knowledge.document(document["id"])
    assert failed["run"] == "FAIL" and "校验失败" in failed["error"]
    assert app.state.db.rows("SELECT id,content FROM chunks WHERE document_id=?", (document["id"],)) == before


def test_migrated_disabled_source_chunks_stay_disabled_after_reparse(workspace):
    app, client, dataset, document, cache, _ = cached_image(workspace)
    cache.write_text(json.dumps([
        {"page_content": "设备续航14小时。", "metadata": {"source_available": True}},
        {"page_content": "停用的旧版指标：设备续航8小时。", "metadata": {"source_available": False}},
    ], ensure_ascii=False), encoding="utf-8")
    app.state.db.execute("UPDATE documents SET metadata=? WHERE id=?", (json.dumps({"imported_text_cache": True,
        "imported_text_sha256": hashlib.sha256(cache.read_bytes()).hexdigest(),
        "ocr_recovery": {"applied": True}}), document["id"]))
    parse(client, dataset, [document])
    for _ in range(2):
        chunks = app.state.db.rows("SELECT content,enabled FROM chunks WHERE document_id=? ORDER BY ordinal", (document["id"],))
        assert [row["enabled"] for row in chunks] == [1, 0]
        result = data(client.post("/api/v1/retrieval", json={"dataset_ids": [dataset], "question": "设备续航", "similarity_threshold": 0}))
        assert len(result["chunks"]) == 1 and "14小时" in result["chunks"][0]["content"]
        assert "待人工核对" in app.state.knowledge.public_document(app.state.knowledge.document(document["id"]))["text_method"]
        data(client.put(f"/api/v1/datasets/{dataset}", json={"parser_config": {"chunk_size": 200, "chunk_overlap": 20}}))
        parse(client, dataset, [document])
