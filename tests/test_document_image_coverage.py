"""Regressions from mixed technical document uploads, using synthetic sources."""
from io import BytesIO
import json
import zipfile
from unittest.mock import Mock

from docx import Document as WordDocument
from docx.oxml import OxmlElement
from langchain_core.documents import Document
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from PIL import Image, ImageDraw
from pptx import Presentation
from pptx.util import Inches
import pytest

from test_backend import data, parse, upload, workspace
from test_drawing_evidence import vision_api

from backend.config import Settings
from backend.collab_graph import CollabEngine, _quantities
from backend.db import Database
from backend.drawing_sources import DrawingPage, drawing_pages
from backend.knowledge import KnowledgeService
from backend.parsers import extract


def picture():
    image = Image.new("RGB", (640, 400), "white")
    ImageDraw.Draw(image).text((40, 80), "H1 H2 50 +/- 0.2 mm", fill="black")
    buffer = BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    return buffer


def test_word_embedded_picture_is_a_visual_source(tmp_path):
    path = tmp_path / "technical.docx"
    document = WordDocument()
    document.add_paragraph("Synthetic design diagram")
    document.add_picture(picture())
    document.save(path)
    pages = list(drawing_pages(path, text_pages=set()))
    assert len(pages) == 1
    assert pages[0].source_part.startswith("word/media/")
    assert pages[0].image.size == (640, 400)
    pages[0].image.close()


def test_image_only_presentation_is_a_visual_source(tmp_path):
    path = tmp_path / "drawing.pptx"
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    slide.shapes.add_picture(picture(), Inches(1), Inches(1))
    deck.save(path)
    pages = list(drawing_pages(path, text_pages=set()))
    assert len(pages) == 1
    assert pages[0].source_part.startswith("ppt/media/")
    assert pages[0].slide_number == 1
    pages[0].image.close()


def test_presentation_reads_labels_inside_grouped_shapes(tmp_path):
    path = tmp_path / "grouped-wiring.pptx"
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    group = slide.shapes.add_group_shape()
    label = group.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1))
    label.text = "Synthetic wiring: connector X1 pin A3 to X2 pin B7"
    deck.save(path)
    assert "X1 pin A3 to X2 pin B7" in extract(path)[0].page_content


def test_scanned_pdf_page_keeps_visual_text_beside_native_page_number(tmp_path, monkeypatch):
    database = Database(tmp_path / "workspace.sqlite3")
    service = KnowledgeService(database, Mock(), Settings(data_dir=tmp_path))
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    source = uploads / "scan.pdf"
    source.write_bytes(b"synthetic source for substituted renderer")
    documents = [Document(page_content="17", metadata={"page_number": 1, "source_kind": "pdf"})]
    row = {"id": "synthetic", "path": source.name}
    monkeypatch.setattr(service, "_current", lambda *_: row)
    monkeypatch.setattr("backend.knowledge.drawing_pages", lambda *args, **kwargs: (page for page in [
        DrawingPage(1, Image.new("RGB", (200, 200), "white")),
    ]))
    monkeypatch.setattr("backend.knowledge.read_drawing_page", lambda *_: (
        "text", "Synthetic contract requires annotated drawings and cable diagrams.", [], [],
    ))
    try:
        service._drawing_parts(row, 1, "synthetic-vision", documents, force_pdf=False)
        assert any("requires annotated drawings" in item.page_content for item in documents)
    finally:
        service.executor.shutdown()


def test_unused_null_office_relationship_does_not_hide_valid_body_text(tmp_path):
    path = tmp_path / "recoverable.docx"
    original = BytesIO()
    document = WordDocument()
    document.add_paragraph("Synthetic native paragraph survives a broken image relationship.")
    document.save(original)
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(path, "w") as destination:
        for item in source.infolist():
            content = source.read(item.filename)
            if item.filename == "word/_rels/document.xml.rels":
                content = content.replace(b"</Relationships>",
                    b'<Relationship Id="rId999" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="NULL"/></Relationships>')
            destination.writestr(item, content)
    result = extract(path)
    assert "survives a broken image relationship" in result[0].page_content
    assert result[0].metadata.get("parser_warnings")


def test_word_content_control_preserves_contents(tmp_path):
    path = tmp_path / "contents.docx"
    document = WordDocument()
    paragraph = document.add_paragraph("Synthetic contents: diagram delivery and cable specification")
    control = OxmlElement("w:sdt")
    content = OxmlElement("w:sdtContent")
    content.append(paragraph._p)
    control.append(content)
    document.element.body.insert(0, control)
    document.save(path)
    assert "Synthetic contents" in extract(path)[0].page_content


def test_word_nested_table_preserves_technical_text(tmp_path):
    path = tmp_path / "nested.docx"
    document = WordDocument()
    outer = document.add_table(rows=1, cols=1)
    inner = outer.cell(0, 0).add_table(rows=1, cols=1)
    inner.cell(0, 0).text = "Synthetic cable pin A3 maps to B7"
    document.save(path)
    assert "pin A3 maps to B7" in extract(path)[0].page_content


@pytest.mark.parametrize("disclaimer", [
    "该数值不是实测记录或验收结论。",
    "该数值不等同于成品实测或验收结论。",
])
def test_collab_accepts_an_explicit_design_only_statement(monkeypatch, disclaimer):
    engine = CollabEngine(None, None)
    monkeypatch.setattr(engine, "_ask", lambda *_: {
        "grounded": True, "issues": [], "supported_ids": [1], "summary": "Synthetic source supports the design observation",
    })
    result = engine.validate({
        "question": "图纸中孔距是多少？", "standalone_question": "图纸中孔距是多少？", "intent": "question",
        "answer": "已确认图纸标注为 50 mm [1]。" + disclaimer,
        "references": [{"document_name": "drawing.png", "content": "孔距设计标注 50 mm",
                        "drawing_evidence": {"status": "confirmed"}}],
    })
    assert result["validation"]["passed"], result["validation"]


def test_collab_generation_receives_all_sources_and_keeps_relevance_as_a_hint(monkeypatch):
    payloads = []
    def answer(prompt):
        payloads.append(json.loads(prompt.messages[-1].content.split("INPUT_JSON:\n", 1)[1]))
        return AIMessage(content="Synthetic supported observation [2]")
    models = Mock()
    models.chat.return_value = RunnableLambda(answer)
    monkeypatch.setattr("backend.collab_graph._writer", lambda *_: None)
    engine = CollabEngine(models, None)
    engine.answer({
        "question": "H1 H2?", "standalone_question": "H1 H2?", "intent": "question", "settings": {},
        "references": [{"document_name": "unrelated.png", "content": "H3 H4"},
                       {"document_name": "relevant.png", "content": "H1 H2"}],
        "evidence_assessment": {"relevant_ids": [2], "sufficient": True},
    })
    assert [row["source_id"] for row in payloads[0]["references"]] == [1, 2]
    assert payloads[0]["evidence_assessment"]["relevant_ids"] == [2]


def test_nominal_dimension_inherits_its_explicit_tolerance_unit():
    assert {("255", "mm"), ("0.8", "mm")} <= _quantities("中心距 255 ± 0.8 mm")
    assert {("254.2", "mm"), ("255.8", "mm")} <= _quantities("范围 254.2 至 255.8 mm")
    assert ("255", "mm") not in _quantities("孔数 255，公差 0.8 mm")
    assert {("252.2", "mm"), ("253.8", "mm")} <= _quantities("图纸标注 253 ± 0.8 mm")


@pytest.mark.parametrize("statement", [
    "协议要求的实测范围须为 254.2 至 255.8 mm [1]。",
    "目前缺少实测记录 [1]。",
    "目前无实测记录 [1]。",
    "成品验收时应提供测量记录，实测范围须为 254.2 至 255.8 mm [1]。",
    "后续应调取安装孔位图，验收阶段再调取实测记录 [1]。",
    "需要成品验收报告或合格证明 [1]。",
    "需要成品验收报告，用以证明验收已通过 [1]。",
])
def test_collab_accepts_requirements_and_missing_measurements(monkeypatch, statement):
    engine = CollabEngine(None, None)
    monkeypatch.setattr(engine, "_ask", lambda *_: {
        "grounded": True, "issues": [], "supported_ids": [1], "summary": "Synthetic requirement, not a measurement",
    })
    result = engine.validate({
        "question": "现有材料能否证明已完成验收？", "standalone_question": "现有材料能否证明已完成验收？", "intent": "review",
        "answer": statement,
        "references": [{"document_name": "requirement.txt",
                        "content": "合成技术协议要求测量记录，允许范围 254.2 至 255.8 mm，当前仅有图纸。"}],
    })
    assert result["validation"]["passed"], result["validation"]


def test_negated_clause_does_not_hide_a_later_measurement_claim(monkeypatch):
    engine = CollabEngine(None, None)
    monkeypatch.setattr(engine, "_ask", lambda *_: {
        "grounded": True, "issues": [], "supported_ids": [1], "summary": "Synthetic validation stub",
    })
    result = engine.validate({
        "question": "是否验收通过？", "standalone_question": "是否验收通过？", "intent": "review",
        "answer": "图纸不是实测记录，但成品实测合格 [1]。",
        "references": [{"document_name": "drawing.png", "content": "设计图纸",
                        "drawing_evidence": {"status": "confirmed"}}],
    })
    assert result["validation"]["passed"] is False


def test_large_technical_word_upload_and_native_parse(workspace):
    _, client = workspace
    from test_backend import first_dataset
    content = BytesIO()
    document = WordDocument()
    document.add_paragraph("Synthetic large technical document with a valid native body.")
    document.save(content)
    with zipfile.ZipFile(content, "a") as archive:
        archive.writestr("unused-test-padding.bin", b"x" * (31 * 1024 * 1024), compress_type=zipfile.ZIP_STORED)
    dataset = first_dataset(client)
    source = upload(client, dataset, "large-synthetic.docx", content.getvalue())
    [parsed] = parse(client, dataset, [source])
    assert parsed["run"] == "DONE"
    assert parsed["size"] > 30 * 1024 * 1024


@pytest.mark.parametrize("extension", ["docx", "pptx"])
def test_office_image_upload_indexes_and_opens_original_image(workspace, vision_api, extension):
    app, client = workspace
    dataset, _ = vision_api
    content = BytesIO()
    if extension == "docx":
        document = WordDocument()
        document.add_picture(picture())
        document.save(content)
    else:
        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        slide.shapes.add_picture(picture(), Inches(1), Inches(1))
        deck.save(content)
    document = upload(client, dataset, "synthetic-drawing." + extension, content.getvalue())
    [parsed] = parse(client, dataset, [document])
    assert parsed["chunk_count"] >= 1
    resource = f"/api/v1/datasets/{dataset}/documents/{document['id']}/drawing-evidence"
    [evidence] = data(client.get(resource))["rows"]
    assert evidence["source_part"]
    original = client.get(resource + "/" + evidence["id"] + "/image")
    assert original.status_code == 200
    with Image.open(BytesIO(original.content)) as image:
        assert image.size == (640, 400)
    result = data(client.post("/api/v1/retrieval", json={
        "dataset_ids": [dataset], "question": "H1 H2", "similarity_threshold": 0,
    }))
    assert result["chunks"][0]["drawing_evidence"]["source_part"] == evidence["source_part"]
