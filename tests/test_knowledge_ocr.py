"""Synthetic, offline checks for optional local Windows OCR recovery."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import zipfile

import pytest

from tools import recover_knowledge_ocr as recovery


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_export_checksums_and_path_boundaries(tmp_path):
    root = tmp_path / "export"
    root.mkdir()
    original = root / "original.png"
    original.write_bytes(b"synthetic-only")
    digest = recovery.file_digest(original)
    assert recovery.verified_path(root, "original.png", digest) == original
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"synthetic-only")
    with pytest.raises(ValueError, match="outside"):
        recovery.verified_path(root, "../outside.png", digest)
    with pytest.raises(ValueError, match="relative"):
        recovery.verified_path(root, str(outside.resolve()), digest)
    original.write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        recovery.verified_path(root, "original.png", digest)


def test_only_missing_clean_source_text_is_selected(tmp_path):
    root = tmp_path.resolve()
    error = ("**ERROR**: Failed to connect to Ollama. Please check that Ollama is "
             "downloaded, running and accessible. https://ollama.com/download")
    records = []
    for number, text in enumerate((error, error + "设备续航14小时。", " ", "Error handling is required.")):
        original = root / f"{number}.png"
        original.write_bytes(b"synthetic-png")
        chunks_path = root / f"{number}.json"
        write_json(chunks_path, [{"content": text}])
        records.append({"id": str(number), "name": f"{number}.png",
                        "original_path": original.name, "original_sha256": recovery.file_digest(original),
                        "chunks_path": chunks_path.name, "chunks_sha256": recovery.file_digest(chunks_path)})
    write_json(root / "export-manifest.json", {"datasets": [{"id": "kb", "documents": records}]})
    assert [item["document_id"] for item in recovery.selected_documents(root)] == ["0", "2"]
    # Preserve genuine OCR immediately after the fixed error, without a newline.
    assert recovery.source_text({"content": error + "设备续航14小时。"}) == "设备续航14小时。"


def test_pptx_uses_presentation_order_for_source_pages(tmp_path):
    path = tmp_path / "synthetic.pptx"
    relationship = recovery.DOC_REL_NS
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ppt/presentation.xml", f'<p:presentation xmlns:p="{recovery.PRESENTATION_NS}" xmlns:r="{relationship}"><p:sldIdLst><p:sldId id="8" r:id="first"/><p:sldId id="9" r:id="second"/></p:sldIdLst></p:presentation>')
        archive.writestr("ppt/_rels/presentation.xml.rels", f'<Relationships xmlns="{recovery.REL_NS}"><Relationship Id="first" Type="{relationship}/slide" Target="slides/slide31.xml"/><Relationship Id="second" Type="{relationship}/slide" Target="slides/slide2.xml"/></Relationships>')
        for name, image in (("slide31.xml", "a.png"), ("slide2.xml", "b.png")):
            archive.writestr("ppt/slides/" + name, f'<p:sld xmlns:p="{recovery.PRESENTATION_NS}" xmlns:a="{recovery.DRAWING_NS}" xmlns:r="{relationship}"><a:blip r:embed="img"/></p:sld>')
            archive.writestr("ppt/slides/_rels/" + name + ".rels", f'<Relationships xmlns="{recovery.REL_NS}"><Relationship Id="img" Type="{relationship}/image" Target="../media/{image}"/><Relationship Id="external" Type="{relationship}/image" Target="https://example.invalid/image" TargetMode="External"/></Relationships>')
            archive.writestr("ppt/media/" + image, b"synthetic-image")
        archive.writestr("ppt/media/unmapped.png", b"synthetic-unmapped")
    with zipfile.ZipFile(path) as archive:
        assert recovery.presentation_image_parts(archive) == [
            ("ppt/media/a.png", 1), ("ppt/media/b.png", 2), ("ppt/media/unmapped.png", None)]


def test_ocr_normalization_keeps_lines_numbers_and_english_words():
    result = {"lines": [{"text": "设 备 续 航 14 小 时"}, {"text": "test report 7\x00"}]}
    assert recovery.normalized_ocr_text(result) == "设备续航 14 小时\ntest report 7"


@pytest.mark.skipif(os.environ.get("RUN_WINDOWS_OCR_TESTS") != "1",
                    reason="Opt-in Windows 5.1 + installed zh-Hans-CN OCR language pack")
def test_installed_windows_ocr_two_image_roundtrip(tmp_path):
    from PIL import Image, ImageDraw, ImageFont
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not powershell.is_file():
        pytest.skip("Windows PowerShell 5.1 is unavailable")
    output = tmp_path / "中文路径"
    output.mkdir()
    image = Image.new("RGB", (900, 220), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts/msyh.ttc"), 42)
    draw.text((35, 40), "设备续航14小时，需提交测试报告。", font=font, fill="black")
    image.save(output / "文字.png")
    Image.new("RGB", (250, 100), "white").save(output / "空白.png")
    requests = {"text": {"id": "text", "path": str(output / "文字.png")},
                "blank": {"id": "blank", "path": str(output / "空白.png")}}
    results = recovery.run_local_ocr(requests, output, powershell, "smoke")
    text = recovery.normalized_ocr_text(results["text"])
    assert results["text"]["status"] == "ok"
    assert "14" in text and "设备" in text
    assert not recovery.normalized_ocr_text(results["blank"])
