"""Recover missing source knowledge text using Windows' installed OCR, offline.

Original exports are read-only. Only <export-dir>/ocr-recovery is written.
The recovery manifest can be consumed separately by the knowledge importer.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile


ENGINE = "Windows.Media.Ocr zh-Hans-CN"
QUALITY = "本机OCR，待人工核对"
SOURCE_ERROR = re.compile(
    r"\*\*ERROR\*\*:\s*Failed to connect to Ollama\. Please check that Ollama is downloaded, running and accessible\.\s*https://ollama\.com/download",
    re.I,
)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".gif"}
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def file_digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    partial.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    partial.replace(path)


def verified_path(root: Path, relative, expected_sha) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("Export contains an invalid relative path")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Export file is missing or outside the export directory")
    if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise ValueError("Export contains an invalid checksum")
    if file_digest(path) != expected_sha:
        raise ValueError("Export file checksum does not match")
    return path


def source_text(chunk: dict) -> str:
    value = chunk.get("content", chunk.get("content_with_weight", ""))
    return SOURCE_ERROR.sub("", value if isinstance(value, str) else "").strip()


def selected_documents(root: Path) -> list[dict]:
    """Select only documents without useful existing text, verifying all files."""
    manifest = json.loads((root / "export-manifest.json").read_text(encoding="utf-8"))
    selected = []
    seen = set()
    for dataset in manifest["datasets"]:
        for document in dataset["documents"]:
            identity = (dataset["id"], document["id"])
            if identity in seen:
                raise ValueError("Duplicate document identity in the export")
            seen.add(identity)
            chunk_path = verified_path(root, document["chunks_path"], document["chunks_sha256"])
            chunks = json.loads(chunk_path.read_text(encoding="utf-8"))
            if not isinstance(chunks, list) or any(not isinstance(chunk, dict) for chunk in chunks):
                raise ValueError("Export chunks must be a list of objects")
            if any(source_text(chunk) for chunk in chunks):
                continue
            original = verified_path(root, document["original_path"], document["original_sha256"])
            selected.append({
                "dataset_id": dataset["id"], "document_id": document["id"],
                "source_original_sha256": document["original_sha256"],
                "original_path": original, "extension": Path(document["name"]).suffix.lower(),
            })
    return selected


def resolve_part(base_part: str, target: str) -> str:
    if not target or "\\" in target or ":" in target:
        raise ValueError("Invalid Office relationship target")
    part = posixpath.normpath(posixpath.join(posixpath.dirname(base_part), target))
    if part.startswith(("../", "/")) or part in {".", ".."}:
        raise ValueError("Office relationship escapes the archive")
    return part


def relationships(archive: zipfile.ZipFile, part: str) -> dict:
    path = PurePosixPath(part)
    rel_part = str(path.parent / "_rels" / (path.name + ".rels"))
    if rel_part not in archive.namelist():
        return {}
    result = {}
    for node in ET.fromstring(archive.read(rel_part)).findall("{" + REL_NS + "}Relationship"):
        if node.get("TargetMode") == "External":
            continue
        result[node.attrib["Id"]] = {
            "part": resolve_part(part, node.attrib["Target"]), "type": node.get("Type", ""),
        }
    return result


def presentation_image_parts(archive: zipfile.ZipFile) -> list[tuple[str, int | None]]:
    """Use presentation order and embedded picture relationships for real pages."""
    names = set(archive.namelist())
    used: list[tuple[str, int | None]] = []
    presentation = "ppt/presentation.xml"
    if presentation in names:
        rels = relationships(archive, presentation)
        tree = ET.fromstring(archive.read(presentation))
        slides = tree.findall(".//{" + PRESENTATION_NS + "}sldId")
        for page, slide in enumerate(slides, 1):
            relationship = rels.get(slide.get("{" + DOC_REL_NS + "}id", ""))
            if not relationship or relationship["part"] not in names:
                continue
            slide_part = relationship["part"]
            slide_rels = relationships(archive, slide_part)
            slide_tree = ET.fromstring(archive.read(slide_part))
            for blip in slide_tree.findall(".//{" + DRAWING_NS + "}blip"):
                image_rel = slide_rels.get(blip.get("{" + DOC_REL_NS + "}embed", ""))
                if image_rel and image_rel["type"].endswith("/image"):
                    pair = (image_rel["part"], page)
                    if pair not in used:
                        used.append(pair)
    # Unmapped embedded images still have honest source part metadata, no page.
    mapped = {part for part, _ in used}
    for part in sorted(names):
        if part.startswith("ppt/media/") and part not in mapped and not part.endswith("/"):
            used.append((part, None))
    return used


def media_requests(document: dict, images_dir: Path) -> tuple[list[dict], str | None]:
    """Return image sources without executing Office files or external links."""
    path, extension = document["original_path"], document["extension"]
    images = []
    if extension in IMAGE_EXTENSIONS:
        return [{"id": document["source_original_sha256"], "path": str(path),
                 "metadata": {"source_kind": "image"}}], None
    if extension not in {".pptx", ".docx"}:
        return [], "此格式未进行本机图片OCR恢复，需要另行解析"
    if not zipfile.is_zipfile(path):
        return [], "原件不是有效的Office ZIP文件，无法读取正文或图片"
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 20000 or sum(info.file_size for info in infos) > 200 * 1024 * 1024:
            return [], "Office展开后超过本机恢复大小上限"
        if extension == ".pptx":
            parts = presentation_image_parts(archive)
        else:
            parts = [(info.filename, None) for info in infos
                     if info.filename.startswith("word/media/") and not info.is_dir()]
        for part, page in parts:
            suffix = PurePosixPath(part).suffix.lower()
            if suffix not in IMAGE_EXTENSIONS:
                continue
            data = archive.read(part)
            digest = hashlib.sha256(data).hexdigest()
            destination = images_dir / (digest + suffix)
            if not destination.is_file():
                destination.write_bytes(data)
            elif file_digest(destination) != digest:
                raise ValueError("Existing recovery image checksum does not match")
            metadata = {"source_kind": "presentation_image" if extension == ".pptx" else "word_image",
                        "source_part": part}
            if page is not None:
                metadata["slide_number"] = page
                metadata["page_number"] = page
                metadata["page_number_kind"] = "presentation_slide"
            images.append({"id": digest, "path": str(destination), "metadata": metadata})
    return images, None if images else "原件无可提取正文，且没有可供本机OCR识别的嵌入图片"


def normalized_ocr_text(result: dict) -> str:
    lines = result.get("lines") or []
    raw_lines = [str(line.get("text", "")) for line in lines if isinstance(line, dict)]
    text = "\n".join(raw_lines) if raw_lines else str(result.get("text", ""))
    # OCR inserts word spaces within Chinese phrases; preserve line boundaries.
    return re.sub(r"(?<=[\u3400-\u9fff])[ \t]+(?=[\u3400-\u9fff])", "", text.replace("\x00", "")).strip()


def run_local_ocr(requests: dict, output: Path, powershell: Path, label: str) -> dict:
    if not requests:
        return {}
    if not powershell.is_file():
        raise ValueError("Windows PowerShell 5.1 was not found")
    request_path = output / (label + "-inputs.json")
    result_path = output / (label + "-results.json")
    save_json(request_path, list(requests.values()))
    process = subprocess.run([
        str(powershell), "-NoProfile", "-NonInteractive", "-File",
        str(Path(__file__).with_name("windows_ocr.ps1").resolve()),
        "-InputManifest", str(request_path), "-OutputPath", str(result_path),
    ], capture_output=True, timeout=max(120, len(requests) * 30),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if process.returncode:
        raise RuntimeError("Windows OCR process failed; verify the local zh-Hans-CN language pack and PS5.1")
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    if payload.get("engine") != ENGINE:
        raise ValueError("Unexpected local OCR engine response")
    results = {item["id"]: item for item in payload["results"]}
    if set(results) != set(requests):
        raise ValueError("Local OCR results do not match image requests")
    return results


def retry_image(request: dict, output: Path) -> tuple[dict, dict]:
    """Retry a blank OCR result on a white, enlarged local derivative."""
    from PIL import Image, ImageOps
    with Image.open(request["path"]) as image:
        rgba = image.convert("RGBA")
        white = Image.new("RGBA", rgba.size, "white")
        prepared = ImageOps.autocontrast(Image.alpha_composite(white, rgba).convert("L"))
        scale = min(2.0, 2400 / max(prepared.size))
        width, height = prepared.size
        prepared = prepared.resize((round(width * scale), round(height * scale)), Image.Resampling.LANCZOS)
        target = output / "preprocessed" / (request["id"] + ".png")
        target.parent.mkdir(exist_ok=True)
        prepared.save(target)
        metadata = {
            "ocr_preprocessing": "white_background_grayscale_autocontrast_upscale",
            "ocr_input_sha256": file_digest(target),
            "original_image_width": width, "original_image_height": height,
            "ocr_scale_x": prepared.width / width, "ocr_scale_y": prepared.height / height,
        }
    return {"id": request["id"], "path": str(target)}, metadata


def recover(export_dir: Path, powershell: Path) -> dict:
    root = export_dir.resolve()
    selected = selected_documents(root)
    output = root / "ocr-recovery"
    output.mkdir(parents=True, exist_ok=True)
    images_dir = output / "images"
    images_dir.mkdir(exist_ok=True)
    prepared = {}
    requests = {}
    for document in selected:
        digest = document["source_original_sha256"]
        if digest not in prepared:
            image_sources, reason = media_requests(document, images_dir)
            prepared[digest] = {"sources": image_sources, "reason": reason}
            for request in image_sources:
                requests.setdefault(request["id"], {"id": request["id"], "path": request["path"]})
    results = run_local_ocr(requests, output, powershell, "ocr-engine")
    retries, preprocessing = {}, {}
    for digest, request in requests.items():
        result = results[digest]
        if result.get("status") == "ok" and not normalized_ocr_text(result):
            retries[digest], preprocessing[digest] = retry_image(request, output)
    retried = run_local_ocr(retries, output, powershell, "ocr-retry")
    for digest, result in retried.items():
        if result.get("status") == "ok" and normalized_ocr_text(result):
            results[digest] = {**result, "preprocessing": preprocessing[digest]}
    summaries = {}
    for digest, group in prepared.items():
        entries, errors = [], []
        for source in group["sources"]:
            result = results[source["id"]]
            if result.get("status") != "ok":
                errors.append("本机OCR未能解码或识别嵌入图片")
                continue
            text = normalized_ocr_text(result)
            if not text:
                continue
            metadata = {
                **source["metadata"], "source_image_sha256": source["id"],
                "ocr_engine": ENGINE, "quality": QUALITY, "requires_human_verification": True,
                "image_width": result.get("width"), "image_height": result.get("height"),
                "ocr_lines": result.get("lines", []),
                "ocr_box_units": "image_pixels_xywh",
            }
            metadata.update(result.get("preprocessing", {}))
            entries.append({"page_content": text, "metadata": metadata})
        entries_path = output / (digest + ".json")
        save_json(entries_path, entries)
        characters = sum(len(entry["page_content"]) for entry in entries)
        reason = group["reason"]
        if not entries and not reason:
            reason = "；".join(sorted(set(errors))) if errors else "图片未识别到文字，可能是图标或没有可辨认正文"
        summaries[digest] = {
            "entries_path": entries_path.relative_to(root).as_posix(),
            "entries_sha256": file_digest(entries_path), "content_characters": characters,
            "quality": QUALITY, "status": "recovered" if entries else "no_text",
            "reason": reason, "warnings": sorted(set(errors)),
        }
    documents = [{
        "dataset_id": document["dataset_id"], "document_id": document["document_id"],
        "source_original_sha256": document["source_original_sha256"],
        **summaries[document["source_original_sha256"]],
    } for document in selected]
    manifest = {"version": 1, "engine": ENGINE, "documents": documents}
    save_json(output / "manifest.json", manifest)
    stats = Counter()
    for document in documents:
        stats[document["status"] + "_documents"] += 1
        stats["content_characters"] += document["content_characters"]
    return {"selected_documents": len(selected), "unique_originals": len(prepared),
            "unique_images": len(requests), "preprocessed_retries": len(retries),
            "ocr_image_errors": sum(item.get("status") != "ok" for item in results.values()),
            **stats, "manifest": str((output / "manifest.json").relative_to(root))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-dir", type=Path, required=True)
    parser.add_argument("--powershell", type=Path, default=Path(os.environ.get("SystemRoot", r"C:\Windows")) /
                        "System32/WindowsPowerShell/v1.0/powershell.exe")
    args = parser.parse_args()
    try:
        print(json.dumps(recover(args.export_dir, args.powershell), ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired) as exc:
        # Do not expose OCR content, document names, or command arguments.
        print("Local OCR recovery failed: " + type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
