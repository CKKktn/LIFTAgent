import json

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from test_backend import workspace, first_dataset
from test_review_engine import engine_workspace
from test_review_jobs import wait_status
from fake_model_provider import answer_for


def test_uploaded_basis_is_separate_from_knowledge_and_cannot_be_indexed(workspace):
    app, client = workspace
    kb = first_dataset(client)
    response = client.post(f"/api/v1/datasets/{kb}/review-bases",
                           files=[("file", ("contract.txt", b"runtime at least 8 hours", "text/plain"))])
    assert response.status_code == 200, response.text
    basis = response.json()["data"][0]
    listing = client.get(f"/api/v1/datasets/{kb}/documents").json()["data"]
    assert listing["total"] == 0
    assert app.state.knowledge.dataset(kb)["document_count"] == 0
    with pytest.raises(Exception, match="审查依据"):
        app.state.knowledge.parse([basis["id"]], kb)
    assert app.state.db.one("SELECT count(*) AS n FROM chunks")["n"] == 0


def test_visual_extraction_uses_vision_and_preserves_source_quote(engine_workspace, monkeypatch):
    w = engine_workspace
    model = w.models.save({"model_type": "vision", "name": "synthetic-vision",
                           "base_url": "https://synthetic.invalid/v1"})
    calls = []
    text = "设备续航时间不得低于8小时。"

    def respond(messages):
        calls.append(messages)
        return AIMessage(content=json.dumps({
            "page_text": text,
            "technical_indicators": [{"type": "numeric", "subject": "设备", "title": "续航时间",
                "criterion": text, "rationale": "明确技术门槛", "source_ids": ["P1"], "quote": text}],
            "closure_conditions": [],
        }, ensure_ascii=False))

    monkeypatch.setattr(w.models, "vision", lambda *args, **kwargs: RunnableLambda(respond))
    monkeypatch.setattr(w.models, "chat", lambda *args, **kwargs: pytest.fail("Extraction called chat model"))
    state = w.engine.prepare({**w.payload, "extraction_vision_model_id": model["id"]})
    result = w.engine.extract_batch(state["report"], state["batches"][0])
    assert result["requirements"][0]["quote"] == text
    assert any(block.get("type") == "image_url" for block in calls[0][-1].content)
    assert state["report"]["extractionVisionModel"] == model["id"]
    assert state["report"]["model"] != model["id"]


def test_visual_basis_rejects_quote_that_is_not_in_transcription(engine_workspace, monkeypatch):
    w = engine_workspace
    model = w.models.save({"model_type": "vision", "name": "synthetic-vision",
                           "base_url": "https://synthetic.invalid/v1"})
    monkeypatch.setattr(w.models, "vision", lambda *args, **kwargs: RunnableLambda(lambda messages:
        AIMessage(content=json.dumps({"page_text": "Visible text", "technical_indicators": [{
            "type": "numeric", "subject": "device", "title": "runtime", "criterion": "8 hours",
            "rationale": "threshold", "source_ids": ["P1"], "quote": "Fabricated quote"}],
            "closure_conditions": []}))))
    state = w.engine.prepare({**w.payload, "extraction_vision_model_id": model["id"]})
    result = w.engine.extract_batch(state["report"], state["batches"][0])
    assert not result["requirements"] and result["rejected"] == 1


def test_visual_pdf_batches_retain_original_page_numbers(engine_workspace):
    from reportlab.pdfgen import canvas
    from backend.review_basis import visual_basis_batches, visual_basis_blocks
    w = engine_workspace
    path = w.knowledge.settings.data_dir / "uploads" / "synthetic-contract.pdf"
    pdf = canvas.Canvas(str(path))
    for number in (1, 2):
        pdf.drawString(60, 720, f"Page {number}: runtime >= 8 hours")
        pdf.showPage()
    pdf.save()
    row = w.knowledge.add_document(w.dataset["id"], path.name, path, path.stat().st_size)
    document = w.knowledge.document(row["id"])
    batches = visual_basis_batches(w.knowledge, document)
    assert [batch["visual_page"] for batch in batches] == [1, 2]
    assert all(not batch["reflow"] for batch in batches)
    assert any(block["type"] == "image_url" for block in visual_basis_blocks(w.knowledge, document, batches[1]))


@pytest.mark.parametrize("with_text", [True, False])
def test_every_office_embedded_image_can_be_read_including_scan_only_documents(engine_workspace, with_text):
    from io import BytesIO
    from docx import Document
    from PIL import Image
    from backend.review_basis import visual_basis_batches, visual_basis_blocks
    w = engine_workspace
    path = w.knowledge.settings.data_dir / "uploads" / "synthetic-scan.docx"
    doc = Document()
    if with_text:
        doc.add_paragraph("Synthetic contract")
    for color in ("red", "blue"):
        stream = BytesIO()
        Image.new("RGB", (120, 120), color).save(stream, format="PNG")
        stream.seek(0)
        doc.add_picture(stream)
    doc.save(path)
    row = w.knowledge.add_document(w.dataset["id"], path.name, path, path.stat().st_size)
    document = w.knowledge.document(row["id"])
    batches = visual_basis_batches(w.knowledge, document)
    images = [batch for batch in batches if batch.get("source_part")]
    assert len(images) == 2
    for batch in images:
        assert any(block["type"] == "image_url" for block in visual_basis_blocks(w.knowledge, document, batch))


def test_visual_upload_confirmation_and_chat_judgment_complete_a_job(workspace, monkeypatch):
    app, client = workspace
    kb = first_dataset(client)
    models, service = app.state.models, app.state.review_jobs
    chat = models.save({"model_type": "chat", "name": "synthetic-chat", "base_url": "https://synthetic.invalid/v1"})
    vision = models.save({"model_type": "vision", "name": "synthetic-vision", "base_url": "https://synthetic.invalid/v1"})
    row = client.post(f"/api/v1/datasets/{kb}/documents",
                     files=[("file", ("方案.txt", "设备续航为10小时。".encode(), "text/plain"))]).json()["data"][0]
    app.state.db.execute("UPDATE documents SET run='RUNNING' WHERE id=?", (row["id"],))
    app.state.knowledge._parse_one(row["id"], app.state.knowledge.document(row["id"])["generation"])
    basis = client.post(f"/api/v1/datasets/{kb}/review-bases",
                       files=[("file", ("合同.txt", "设备续航不得低于8小时。".encode(), "text/plain"))]).json()["data"][0]
    duplicate = client.post(f"/api/v1/datasets/{kb}/documents",
                           files=[("file", ("旧合同副本.txt", "设备续航不得低于8小时。".encode(), "text/plain"))]).json()["data"][0]
    app.state.db.execute("UPDATE documents SET run='RUNNING' WHERE id=?", (duplicate["id"],))
    app.state.knowledge._parse_one(duplicate["id"], app.state.knowledge.document(duplicate["id"])["generation"])
    vision_calls, chat_calls = [], []

    def see(messages):
        vision_calls.append(messages)
        return AIMessage(content=json.dumps({"page_text": "设备续航不得低于8小时。",
            "technical_indicators": [{"type": "numeric", "subject": "设备续航", "title": "设备续航",
                "criterion": "设备续航不得低于8小时。", "rationale": "性能门槛", "source_ids": ["P1"],
                "quote": "设备续航不得低于8小时。"}], "closure_conditions": []}, ensure_ascii=False))

    def judge(prompt):
        messages = [{"role": "user", "content": message.content} for message in prompt.to_messages()]
        chat_calls.append(messages)
        return AIMessage(content=answer_for(messages))

    monkeypatch.setattr(models, "vision", lambda *args, **kwargs: RunnableLambda(see))
    monkeypatch.setattr(models, "chat", lambda *args, **kwargs: RunnableLambda(judge))
    payload = {"kb": kb, "title": "双模型合成核查", "baseline_document_ids": [basis["id"]],
               "model_id": chat["id"], "extraction_vision_model_id": vision["id"]}
    job = service.create("owner", payload)
    waiting = wait_status(service, job["id"], "waiting_confirmation")
    assert len(vision_calls) == 1 and not chat_calls
    assert waiting["report"]["extractionVisionModel"] == vision["id"]
    assert waiting["report"]["model"] == chat["id"]
    assert duplicate["id"] in waiting["report"].get("baselineCopyIds", [])
    service.resume(job["id"], "owner", {"revision": waiting["revision"],
                                        "requirements": waiting["report"]["requirements"]})
    finished = wait_status(service, job["id"], "completed")
    assert finished["report"]["results"][0]["status"] == "已满足"
    assert all(item["source"]["document_id"] != duplicate["id"]
               for item in finished["report"]["results"][0]["evidence"])
    assert len(vision_calls) == 1 and chat_calls
    assert app.state.knowledge.dataset(kb)["document_count"] == 2
    assert app.state.db.one("SELECT count(*) AS n FROM chunks WHERE document_id=?", (basis["id"],))["n"] == 0
