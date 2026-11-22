"""Drawing evidence through the real upload, compatible API and review services."""
from io import BytesIO
import json

from PIL import Image, ImageDraw
import pytest

from backend.collab_graph import CollabEngine
from backend.review_engine import ReviewEngine
import fake_model_provider as provider_module
from fake_model_provider import FakeProvider
from test_backend import data, first_dataset, parse, upload, workspace


DRAWING = {
    "drawing_number": "A-03", "revision": "B", "view": "主视图",
    "object": "安装板 H1、H2 两孔", "annotation": "50 ± 0.2 mm", "meaning": "两孔中心距",
    "region_id": "page", "annotation_box": [200, 200, 380, 265],
    "object_box": [250, 420, 770, 680],
}


def drawing_file(kind):
    image = Image.new("RGB", (800, 600), "white")
    draw = ImageDraw.Draw(image)
    draw.text((160, 130), "50 +/- 0.2 mm", fill="black")
    draw.rectangle((180, 230, 620, 410), outline="black", width=3)
    draw.ellipse((200, 260, 240, 300), outline="black", width=3)
    draw.ellipse((570, 260, 610, 300), outline="black", width=3)
    buffer = BytesIO()
    image.save(buffer, format=kind)
    return buffer.getvalue()


@pytest.fixture
def vision_api(workspace, monkeypatch):
    _, client = workspace
    captured = []
    original = provider_module.answer_for

    def answer(messages):
        blocks = messages[-1]["content"]
        if isinstance(blocks, list):
            captured.append(blocks)
            return json.dumps({"page_kind": "drawing", "page_text": "", "annotations": [DRAWING]}, ensure_ascii=False)
        return original(messages)

    monkeypatch.setattr(provider_module, "answer_for", answer)
    with FakeProvider() as api:
        model = client.post("/settings/models", json={
            "name": "drawing-fixture", "base_url": api.base_url, "model_type": "vision", "api_key": "test-only",
        })
        assert model.status_code == 200, model.text
        dataset = first_dataset(client)
        configured = data(client.put(f"/api/v1/datasets/{dataset}", json={
            "parser_config": {"drawing_vision_model": model.json()["id"]},
        }))
        assert configured["parser_config"]["drawing_vision_model"] == model.json()["id"]
        yield dataset, captured


@pytest.mark.parametrize(("kind", "extension"), [("PNG", "png"), ("PDF", "pdf")])
def test_drawing_upload_indexes_a_locatable_annotation(workspace, vision_api, kind, extension):
    app, client = workspace
    dataset, captured = vision_api
    source = drawing_file(kind)
    doc = upload(client, dataset, "安装板." + extension, source)
    [parsed] = parse(client, dataset, [doc])
    assert parsed["chunk_count"] == 1
    assert len(captured) == 1
    assert any(item.get("image_url", {}).get("url", "").startswith("data:image/") for item in captured[0])
    assert all("ocr_words" not in str(block.get("text", "")) for block in captured[0])

    resource = f"/api/v1/datasets/{dataset}/documents/{doc['id']}/drawing-evidence"
    result = data(client.get(resource))
    assert result["total"] == 1
    [row] = result["rows"]
    assert row["method"] == "vision"
    assert row["status"] == "pending"
    assert row["page_number"] == 1
    assert row["annotation_box"] == [0.2, 0.2, 0.38, 0.265]
    assert row["object_box"] == [0.25, 0.42, 0.77, 0.68]
    retrieved = data(client.post("/api/v1/retrieval", json={
        "dataset_ids": [dataset], "question": "A-03 安装板 H1 H2 两孔中心距", "similarity_threshold": 0,
    }))["chunks"]
    assert retrieved[0]["drawing_evidence"] == row
    assert "50 ± 0.2 mm" in retrieved[0]["content"]
    assert client.get(f"/api/v1/documents/{doc['id']}/preview").content == source
    collab = CollabEngine(app.state.models, app.state.retrieval)
    found = collab.retrieve({"dataset_ids": [dataset], "standalone_question": "A-03 安装板 H1 H2 两孔中心距",
                             "settings": {"similarity_threshold": 0}})
    assert found["references"][0]["drawing_evidence"] == row


def test_correction_reindexes_the_record_and_rejects_a_stale_version(workspace, vision_api):
    _, client = workspace
    dataset, _ = vision_api
    doc = upload(client, dataset, "安装板.png", drawing_file("PNG"))
    parse(client, dataset, [doc])
    resource = f"/api/v1/datasets/{dataset}/documents/{doc['id']}/drawing-evidence"
    [old] = data(client.get(resource))["rows"]
    row_url = f"{resource}/{old['id']}"
    fixed = data(client.patch(row_url, json={
        "version": old["version"], "status": "confirmed", "annotation": "50 ± 0.02 mm",
    }))
    assert fixed["status"] == "confirmed"
    assert fixed["version"] == old["version"] + 1
    assert fixed["annotation_box"] == old["annotation_box"]
    assert data(client.get(row_url)) == fixed
    stale = client.patch(row_url, json={"version": old["version"], "status": "rejected"})
    assert stale.status_code == 409
    search = {"dataset_ids": [dataset], "question": "安装板中心距 0.02 mm", "similarity_threshold": 0}
    [indexed] = data(client.post("/api/v1/retrieval", json=search))["chunks"]
    assert indexed["drawing_evidence"]["annotation"] == "50 ± 0.02 mm"
    assert "50 ± 0.02 mm" in indexed["content"]
    parse(client, dataset, [doc])
    assert data(client.get(resource))["rows"] == [fixed]
    data(client.patch(row_url, json={"version": fixed["version"], "status": "rejected"}))
    parse(client, dataset, [doc])
    assert data(client.post("/api/v1/retrieval", json=search))["chunks"] == []
    rejected = data(client.get(row_url))
    data(client.patch(row_url, json={"version": rejected["version"], "status": "confirmed"}))
    [restored] = data(client.post("/api/v1/retrieval", json=search))["chunks"]
    assert restored["source_available"] is True


def test_confirmation_needs_two_visible_regions(workspace, vision_api):
    _, client = workspace
    dataset, _ = vision_api
    document = upload(client, dataset, "安装板.png", drawing_file("PNG"))
    parse(client, dataset, [document])
    resource = f"/api/v1/datasets/{dataset}/documents/{document['id']}/drawing-evidence"
    [row] = data(client.get(resource))["rows"]
    patch = {"version": row["version"], "status": "confirmed", "object_box": None}
    failed = client.patch(f"{resource}/{row['id']}", json=patch)
    assert failed.status_code == 400
    assert data(client.get(f"{resource}/{row['id']}")) == row
    patch["object_box"] = [0.22, 0.42, 0.82, 0.75]
    updated = data(client.patch(f"{resource}/{row['id']}", json=patch))
    assert updated["status"] == "confirmed"
    assert updated["object_box"] == [0.22, 0.42, 0.82, 0.75]


def test_text_upload_keeps_using_the_text_parser(workspace, vision_api):
    _, client = workspace
    dataset, captured = vision_api
    document = upload(client, dataset, "说明.txt", "合成说明，文本解析验证".encode())
    [parsed] = parse(client, dataset, [document])
    assert parsed["chunk_count"] == 1
    assert captured == []


@pytest.mark.parametrize(("evidence_status", "criterion", "verdict"), [
    ("pending", "图纸中 H1、H2 中心距应标注为 50 ± 0.2 mm", "待人工复核"),
    ("confirmed", "图纸中 H1、H2 中心距应标注为 50 ± 0.2 mm", "已满足"),
    ("confirmed", "验收时应实测 H1、H2 中心距，要求 50 ± 0.2 mm", "证据不足"),
    ("confirmed", "H1、H2 中心距须在实物上用卡尺测得，要求 50 ± 0.2 mm", "证据不足"),
])
def test_quick_judgment_enforces_drawing_evidence_scope(monkeypatch, evidence_status, criterion, verdict):
    engine = ReviewEngine(None, None, None, None, None)
    monkeypatch.setattr(engine, "_ask", lambda *_: {
        "status": "已满足", "analysis": "指标符合 E1。", "gaps": "", "project_plan": "", "recommendation": "",
        "evidence": [{"source_id": "E1", "kind": "支持", "basis": "可比实测", "analysis": "E1 的中心距匹配。"}],
    })
    location = {"id": "synthetic", "status": evidence_status, "annotation": "50 ± 0.2 mm",
                "annotation_box": [0.2, 0.2, 0.38, 0.265], "object_box": [0.25, 0.42, 0.77, 0.68]}
    result = engine.judge({}, {"type": "numeric", "title": "孔中心距", "criterion": criterion}, [{
        "reviewId": "E1", "document_name": "安装板.png", "content": "图纸标注 50 ± 0.2 mm",
        "source_kind": "drawing_annotation", "drawing_evidence": location,
    }])
    assert result["status"] == verdict
    assert result["evidence"][0]["basis"] == "图纸标注"
    assert result["evidence"][0]["source"]["drawing_evidence"] == location


def test_pending_drawing_does_not_override_applicable_measured_counterevidence(monkeypatch):
    engine = ReviewEngine(None, None, None, None, None)
    monkeypatch.setattr(engine, "_ask", lambda *_: {
        "status": "已满足", "analysis": "E1 的值符合。", "gaps": "", "project_plan": "", "recommendation": "",
        "evidence": [{"source_id": "E1", "kind": "支持", "analysis": "符合"},
                     {"source_id": "E2", "kind": "反证", "basis": "可比实测", "analysis": "实测偏小"}],
    })
    result = engine.judge({}, {"type": "numeric", "criterion": "H1、H2 孔距须不低于 50 mm"}, [{
        "reviewId": "E1", "document_name": "图纸.png", "content": "标注 50 mm", "drawing_evidence": {"status": "pending"},
    }, {
        "reviewId": "E2", "document_name": "检测记录.pdf", "content": "H1、H2 孔距实测值 44 mm",
    }])
    assert result["status"] == "不满足"
    assert result["evidence"][0]["kind"] == "背景"
    assert "实测偏小" in result["analysis"]


def test_quick_review_prevents_measured_claim_generated_from_confirmed_design(monkeypatch):
    engine = ReviewEngine(None, None, None, None, None)
    monkeypatch.setattr(engine, "_ask", lambda *_: {
        "status": "已满足", "analysis": "实测为 50 mm（E1）。", "gaps": "", "project_plan": "", "recommendation": "",
        "evidence": [{"source_id": "E1", "kind": "支持", "analysis": "实测为 50 mm。"}],
    })
    result = engine.judge({}, {"type": "numeric", "criterion": "图纸应标注 50 mm"}, [{
        "reviewId": "E1", "document_name": "图纸.png", "content": "标注 50 mm",
        "drawing_evidence": {"status": "confirmed"},
    }])
    assert result["status"] == "待人工复核"
    assert "实测为 50 mm" not in json.dumps(result, ensure_ascii=False)


def test_collab_validates_a_verdict_that_cites_a_pending_drawing_in_previous_sentence(monkeypatch):
    engine = CollabEngine(None, None)
    monkeypatch.setattr(engine, "_ask", lambda *_: {"grounded": True, "issues": [], "supported_ids": [1], "summary": "核对"})
    result = engine.validate({
        "question": "孔距是否符合？", "standalone_question": "孔距是否符合？", "intent": "review",
        "answer": "候选读数为 50 mm [1]。结论为符合。",
        "references": [{"document_name": "图纸.png", "content": "标注 50 mm",
                        "drawing_evidence": {"status": "pending"}}],
    })
    assert result["validation"]["passed"] is False
    assert any("待核对" in issue for issue in result["validation"]["issues"])
