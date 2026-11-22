from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from io import BytesIO
import hashlib
import json
import math
from pathlib import Path
import re
import threading

from fastapi import HTTPException
from langchain_core.documents import Document

from .config import Settings
from .db import Database, dumps, now, uid
from .drawing_evidence import (
    DRAWING_KIND, TRANSCRIPTION_KIND, DrawingReviewInput, drawing_text, read_drawing_page, same_drawing_mark,
)
from .drawing_sources import DRAWING_EXTENSIONS, DRAWING_IMAGE_EXTENSIONS, drawing_pages
from .models import ModelRegistry, provider_error
from .parsers import CHUNK_METHODS, extract, split_documents, text_preview_pdf


class ReviewSourceChanged(ValueError):
    pass


class ReviewSourceTooLarge(ValueError):
    pass


class KnowledgeService:
    def __init__(self, db: Database, models: ModelRegistry, settings: Settings):
        self.db, self.models, self.settings = db, models, settings
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="document-parser")
        self.preview_lock = threading.Lock()

    def dataset(self, identifier):
        value = self.db.json_get("datasets", identifier)
        if not value:
            raise HTTPException(404, "知识库不存在")
        stats = self.db.one("SELECT COUNT(*) AS docs, COALESCE(SUM(chunk_count),0) AS chunks FROM documents WHERE dataset_id=?", (identifier,))
        return {**value, "document_count": stats["docs"], "chunk_count": stats["chunks"]}

    def save_dataset(self, body, identifier=None):
        previous = self.dataset(identifier) if identifier else None
        name = str(body.get("name", previous["name"] if previous else "")).strip()
        if not name or len(name) > 200:
            raise HTTPException(400, "知识库名称不能为空，且不得超过 200 字")
        method = body.get("chunk_method", previous.get("chunk_method", "naive") if previous else "naive")
        if method not in CHUNK_METHODS:
            raise HTTPException(400, "请选择通用、整篇、法律条款或表格分块方法")
        embedding = self.models.find(body.get("embedding_model") or (previous.get("embedding_model") if previous else ""), "embedding")
        config = dict(previous.get("parser_config", {})) if previous else {"chunk_size": 1000, "chunk_overlap": 120}
        config.update(body.get("parser_config") or {})
        if "drawing_vision_model" in (body.get("parser_config") or {}):
            vision_model = config["drawing_vision_model"]
            if not isinstance(vision_model, str):
                raise HTTPException(400, "请在模型设置中配置视觉模型后选择")
            config["drawing_vision_model"] = self.models.vision_config(vision_model)["id"] if vision_model else ""
        if not 200 <= int(config.get("chunk_size", 1000)) <= 6000 or not 0 <= int(config.get("chunk_overlap", 120)) < int(config.get("chunk_size", 1000)):
            raise HTTPException(400, "分块长度应为 200–6000，重叠长度应小于分块长度")
        value = {**(previous or {}), "id": identifier or uid(), "name": name,
            "description": str(body.get("description", previous.get("description", "") if previous else ""))[:4000],
            "chunk_method": method, "embedding_model": embedding["id"], "parser_config": config,
            "pipeline_id": "", "create_time": previous["create_time"] if previous else now(), "update_time": now()}
        changed = previous and any(value[field] != previous.get(field) for field in ("chunk_method", "embedding_model", "parser_config"))
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO datasets(id,payload) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (value["id"], dumps(value)))
            if changed:
                connection.execute("UPDATE documents SET run='UNSTART', generation=generation+1, error='解析配置变更，请重新解析' WHERE dataset_id=?", (identifier,))
        return self.dataset(value["id"])

    def document(self, identifier, dataset_id=None):
        row = self.db.one("SELECT * FROM documents WHERE id=?", (identifier,))
        if not row or (dataset_id and row["dataset_id"] != dataset_id):
            raise HTTPException(404, "文档不存在或不属于该知识库")
        return row

    def public_document(self, row):
        meta = json.loads(row["metadata"])
        dataset = self.db.json_get("datasets", row["dataset_id"]) or {}
        warnings = meta.get("drawing_warnings", []) if row["run"] == "DONE" else []
        progress = row["error"] or {"DONE": "解析完成", "RUNNING": "正在解析与建立索引", "UNSTART": "等待解析",
                                    "CANCEL": "已取消"}.get(row["run"], "解析失败")
        if warnings:
            progress += "\n" + "\n".join(warnings)
        return {"id": row["id"], "name": row["name"], "dataset_id": row["dataset_id"],
            "create_time": row["created_at"], "create_date": row["created_at"], "update_time": row["updated_at"],
            "size": row["size"], "chunk_count": row["chunk_count"], "run": row["run"],
            "status": "1" if row["enabled"] else "0", "chunk_method": dataset.get("chunk_method", "naive"),
            "parser_config": {**dataset.get("parser_config", {}), **meta.get("parser_config", {})},
            "draw_every_pdf_page": bool(meta.get("draw_every_pdf_page")),
            "progress": 1 if row["run"] == "DONE" else 0, "progress_msg": progress, "drawing_warnings": warnings,
            "text_method": meta.get("text_method", "本地解析")}

    def file_path(self, row):
        root = (self.settings.data_dir / "uploads").resolve()
        path = (root / row["path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404, "原文件不存在")
        return path

    def _imported_text_path(self, row):
        root = (self.settings.data_dir / "imported-text").resolve()
        path = (root / (row["id"] + ".json")).resolve()
        if not root.is_relative_to(self.settings.data_dir.resolve()) or not path.is_relative_to(root) or not path.is_file():
            raise ValueError("迁移的解析文字缓存不存在，请恢复备份或重新导入")
        return path

    def text_documents(self, row, *, allow_empty=False):
        metadata = json.loads(row["metadata"])
        if not metadata.get("imported_text_cache"):
            options = {"allow_empty": True} if allow_empty else {}
            return extract(self.file_path(row), self.settings.max_document_characters, self.settings.libreoffice_path, **options)
        path = self._imported_text_path(row)
        if path.stat().st_size > self.settings.max_document_characters * 8 + 1024 * 1024:
            raise ValueError("迁移的解析文字缓存过大，请拆分材料")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != metadata.get("imported_text_sha256"):
            raise ValueError("迁移的解析文字缓存校验失败，请恢复备份或重新导入")
        values = json.loads(raw)
        if not isinstance(values, list) or any(not isinstance(item, dict) or not isinstance(item.get("page_content"), str)
            or not isinstance(item.get("metadata", {}), dict) for item in values):
            raise ValueError("迁移的解析文字缓存格式无效")
        documents = [Document(page_content=item["page_content"].replace("\x00", "").strip(), metadata=item.get("metadata", {}))
            for item in values if item["page_content"].strip()]
        if not documents:
            raise ValueError("原知识库没有可用于检索的解析文字；请先为原件完成 OCR 或文字提取")
        if sum(len(item.page_content) for item in documents) > self.settings.max_document_characters:
            raise ValueError("迁移的解析文字超过两百万字符，请拆分材料")
        return documents

    def _review_source_version(self, row):
        metadata = json.loads(row["metadata"])
        imported = bool(metadata.get("imported_text_cache"))
        path = self._imported_text_path(row) if imported else self.file_path(row)
        stat = path.stat()
        version = ["review-text-v1", imported, path.name, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                   metadata.get("imported_text_sha256") if imported else None,
                   self.settings.max_document_characters, self.settings.libreoffice_path]
        return hashlib.sha256(dumps(version).encode()).hexdigest()

    def _review_source_rows(self, row, version, documents):
        paragraphs = []
        for document in documents:
            page = dumps(document.metadata.get("page_number"))
            for line in re.split(r"\n+", document.page_content):
                if not line.strip():
                    continue
                for start in range(0, len(line), 600):
                    if len(paragraphs) == 20000:
                        return None
                    paragraphs.append((row["id"], len(paragraphs), version, line[start:start + 600], page))
        return paragraphs

    def _replace_review_source(self, connection, row, version, paragraphs):
        current = connection.execute("SELECT * FROM documents WHERE id=?", (row["id"],)).fetchone()
        if not current or self._review_source_version(current) != version:
            raise ReviewSourceChanged("审查依据已变更，请重新准备审查任务")
        connection.execute("DELETE FROM review_source_paragraphs WHERE document_id=?", (row["id"],))
        if paragraphs:
            connection.executemany("INSERT INTO review_source_paragraphs(document_id,ordinal,source_version,content,page_number) VALUES (?,?,?,?,?)", paragraphs)

    def _save_review_source(self, row, version, documents):
        paragraphs = self._review_source_rows(row, version, documents)
        if paragraphs is None:
            return False
        with self.db.transaction() as connection:
            self._replace_review_source(connection, row, version, paragraphs)
        return True

    def prepare_review_source(self, row):
        """Return ordered paragraph lengths for batching, reusing indexed source text."""
        version = self._review_source_version(row)
        query = "SELECT length(content) AS characters FROM review_source_paragraphs WHERE document_id=? AND source_version=? ORDER BY ordinal"
        parameters = (row["id"], version)
        sizes = self.db.rows(query, parameters)
        if not sizes:
            if not self._save_review_source(row, version, self.text_documents(row)):
                raise ReviewSourceTooLarge("依据文件分段过多，请拆分合同后审查")
            sizes = self.db.rows(query, parameters)
        if self._review_source_version(row) != version:
            raise ReviewSourceChanged("审查依据已变更，请重新准备审查任务")
        return version, [item["characters"] for item in sizes]

    def review_source_batch(self, row, version, start, end):
        # Batches saved before source caching can rebuild the text once on resume.
        if version is None:
            version, _ = self.prepare_review_source(row)
        if self._review_source_version(row) != version:
            raise ReviewSourceChanged("审查依据已变更，请重新准备审查任务")
        paragraphs = self.db.rows("""SELECT ordinal,content,page_number FROM review_source_paragraphs
            WHERE document_id=? AND source_version=? AND ordinal>=? AND ordinal<? ORDER BY ordinal""",
            (row["id"], version, start, end))
        if len(paragraphs) != end - start or self._review_source_version(row) != version:
            raise ReviewSourceChanged("审查依据分段已变更或缺失，请重新准备审查任务")
        return [{"id": "P" + str(item["ordinal"] + 1), "text": item["content"],
                 "page_number": json.loads(item["page_number"])} for item in paragraphs]

    def add_document(self, dataset_id, name, path, size):
        return self.add_documents(dataset_id, [(name, path, size)])[0]

    def add_documents(self, dataset_id, entries):
        rows = []
        with self.db.transaction() as connection:
            if not connection.execute("SELECT id FROM datasets WHERE id=?", (dataset_id,)).fetchone():
                raise HTTPException(404, "知识库不存在，未保存本批文件")
            for name, path, size in entries:
                identifier, timestamp = uid(), now()
                connection.execute("INSERT INTO documents(id,dataset_id,name,path,extension,size,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (identifier, dataset_id, name, path.name, path.suffix.lower(), size, timestamp, timestamp))
                rows.append(dict(connection.execute("SELECT * FROM documents WHERE id=?", (identifier,)).fetchone()))
        return [self.public_document(row) for row in rows]

    def update_document(self, identifier, body, dataset_id):
        row = self.document(identifier, dataset_id)
        name = str(body.get("name", row["name"])).strip()
        if not name or len(name) > 500:
            raise HTTPException(400, "文档名称无效")
        metadata = json.loads(row["metadata"])
        changed = False
        if "draw_every_pdf_page" in body:
            force = body["draw_every_pdf_page"]
            if row["extension"] != ".pdf" or type(force) is not bool:
                raise HTTPException(400, "只有 PDF 文件可设置全页图纸识别")
            changed = force != bool(metadata.get("draw_every_pdf_page"))
            metadata["draw_every_pdf_page"] = force
        if "parser_config" in body:
            config = body["parser_config"]
            if not isinstance(config, dict):
                raise HTTPException(400, "文件解析配置必须为对象")
            effective = {**self.dataset(dataset_id)["parser_config"], **config}
            try:
                size, overlap = int(effective.get("chunk_size", 1000)), int(effective.get("chunk_overlap", 120))
            except (TypeError, ValueError):
                raise HTTPException(400, "分块长度和重叠长度必须为整数") from None
            if not 200 <= size <= 6000 or not 0 <= overlap < size:
                raise HTTPException(400, "分块长度应为 200–6000，重叠长度应小于分块长度")
            changed = changed or config != metadata.get("parser_config", {})
            metadata["parser_config"] = config
        enabled = int(bool(body["enabled"])) if "enabled" in body else row["enabled"]
        with self.db.transaction() as connection:
            updated = connection.execute("UPDATE documents SET name=?,enabled=?,metadata=?,updated_at=? WHERE id=? AND dataset_id=? AND generation=? AND updated_at=?", (name, enabled, dumps(metadata), now(), identifier, dataset_id, row["generation"], row["updated_at"]))
            if not updated.rowcount:
                raise HTTPException(409, "文件已被其他操作修改，请刷新后重试")
            if changed:
                connection.execute("UPDATE documents SET run='UNSTART',generation=generation+1,error='文件解析配置变更，请重新解析' WHERE id=?", (identifier,))
        return self.public_document(self.document(identifier))

    def remove_documents(self, identifiers, dataset_id):
        rows = [self.document(identifier, dataset_id) for identifier in identifiers]
        with self.db.transaction() as connection:
            for row in rows:
                connection.execute("DELETE FROM documents WHERE id=?", (row["id"],))
        self._remove_files(rows)

    def remove_datasets(self, identifiers):
        rows = []
        with self.db.transaction() as connection:
            # Validate the entire batch before changing any document or file.
            for identifier in identifiers:
                if not connection.execute("SELECT id FROM datasets WHERE id=?", (identifier,)).fetchone():
                    raise HTTPException(404, "部分知识库不存在，未执行删除")
                rows.extend(connection.execute("SELECT * FROM documents WHERE dataset_id=?", (identifier,)).fetchall())
            for identifier in identifiers:
                connection.execute("DELETE FROM datasets WHERE id=?", (identifier,))
            for row in connection.execute("SELECT id,payload FROM chats").fetchall():
                value = json.loads(row["payload"])
                value["dataset_ids"] = [item for item in value.get("dataset_ids", []) if item not in identifiers]
                connection.execute("UPDATE chats SET payload=? WHERE id=?", (dumps(value), row["id"]))
        self._remove_files(rows)

    def _remove_files(self, rows):
        for row in rows:
            path = (self.settings.data_dir / "uploads" / row["path"]).resolve()
            if path.is_relative_to((self.settings.data_dir / "uploads").resolve()):
                path.unlink(missing_ok=True)
            (self.settings.data_dir / "previews" / (row["id"] + ".pdf")).unlink(missing_ok=True)
            cache_root = (self.settings.data_dir / "imported-text").resolve()
            cached = (cache_root / (row["id"] + ".json")).resolve()
            if cache_root.is_relative_to(self.settings.data_dir.resolve()) and cached.is_relative_to(cache_root):
                cached.unlink(missing_ok=True)

    def parse(self, identifiers, dataset_id=None, cancel=False):
        jobs = []
        with self.db.transaction() as connection:
            for identifier in identifiers:
                row = connection.execute("SELECT * FROM documents WHERE id=?", (identifier,)).fetchone()
                if not row or (dataset_id and row["dataset_id"] != dataset_id):
                    raise HTTPException(404, "部分文档不存在或不属于此知识库")
                if row["run"] == "RUNNING" and not cancel:
                    continue
                generation = row["generation"] + 1
                connection.execute("UPDATE documents SET run=?,error='',generation=?,updated_at=? WHERE id=?", ("CANCEL" if cancel else "RUNNING", generation, now(), identifier))
                if not cancel:
                    jobs.append((identifier, generation))
        for identifier, generation in jobs:
            self.executor.submit(self._parse_one, identifier, generation)

    def _current(self, identifier, generation):
        row = self.db.one("SELECT * FROM documents WHERE id=?", (identifier,))
        return row if row and row["generation"] == generation and row["run"] == "RUNNING" else None

    def _drawing_parts(self, row, generation, vision_model, documents, *, force_pdf):
        source = self.file_path(row)
        native_pages = {item.metadata["page_number"]: item for item in documents if "page_number" in item.metadata}
        candidates = []
        warnings = [message for item in documents for message in item.metadata.get("parser_warnings", [])]
        model = None
        with source.open("rb") as stream:
            source_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
        edited = self.db.rows("""SELECT metadata FROM chunks WHERE document_id=?
                               AND json_extract(metadata, '$.drawing_evidence.version') > 1 ORDER BY ordinal""",
                              (row["id"],))
        reviewed = [json.loads(item["metadata"])["drawing_evidence"] for item in edited]
        reviewed = [item for item in reviewed if item.get("source_sha256") == source_sha256]
        with closing(drawing_pages(source, text_pages=set(native_pages), force_pdf=force_pdf, warnings=warnings)) as pages:
            for page in pages:
                with page.image:
                    if not self._current(row["id"], generation):
                        break
                    if model is None:
                        model = self.models.vision(vision_model, temperature=0, max_tokens=8192)
                        model = model.model_copy(update={"streaming": False})
                    kind, text, records, page_warnings = read_drawing_page(model, page)
                    location = ({"source_part": page.source_part, "location_kind": "embedded_image",
                                 "slide_number": page.slide_number} if page.source_part else {})
                warnings.extend(f"第 {page.number} 页 {warning}" for warning in page_warnings)
                if kind == "drawing" and not page.source_part and page.number in native_pages:
                    native_pages[page.number].metadata["drawing_page_text"] = True
                if records:
                    for record in records:
                        recognition = [record[key] for key in ("drawing_number", "revision", "view", "object",
                                                               "annotation", "annotation_box")]
                        record.update(id=uid(), source_sha256=source_sha256, **location,
                                      recognition_key=hashlib.sha256(dumps(recognition).encode()).hexdigest())
                        if not any(same_drawing_mark(item, record) for item in reviewed):
                            candidates.append(record)
                elif kind == "drawing":
                    warnings.append(f"第 {page.number} 页未识别到可保存的图纸标注")
                native = native_pages.get(page.number) if not page.source_part else None
                if text and not records and (native is None or text.strip() not in native.page_content):
                    documents.append(Document(page_content=text, metadata={
                        "page_number": page.number, "source_kind": TRANSCRIPTION_KIND, **location,
                        **({"drawing_page_text": True} if kind == "drawing" else {}),
                    }))
        documents.sort(key=lambda document: document.metadata.get("page_number", 0))
        marks = sorted([*reviewed, *candidates], key=lambda item: item["page_number"])
        drawings = [Document(page_content=drawing_text(mark), metadata={
            "page_number": mark["page_number"], "source_kind": DRAWING_KIND, "drawing_evidence": mark,
            "source_available": mark["status"] != "rejected",
        }) for mark in marks]
        if sum(len(item.page_content) for item in [*documents, *drawings]) > self.settings.max_document_characters:
            raise ValueError("识别和提取的文字过长，请拆分文件后解析")
        return drawings, warnings

    def _parse_one(self, identifier, generation):
        try:
            row = self._current(identifier, generation)
            if not row:
                return
            dataset = self.dataset(row["dataset_id"])
            source_version = self._review_source_version(row)
            metadata = json.loads(row["metadata"])
            media = row["extension"] in DRAWING_EXTENSIONS and not metadata.get("imported_text_cache")
            vision_model = dataset["parser_config"].get("drawing_vision_model") if media else ""
            raster = row["extension"] in DRAWING_IMAGE_EXTENSIONS and media
            if raster and not vision_model:
                raise ValueError("请先在知识库配置中选择支持图像输入的图纸识别模型，再解析图片")
            documents = [] if raster else self.text_documents(row, **({"allow_empty": True} if vision_model else {}))
            drawings, warnings = self._drawing_parts(
                row, generation, vision_model, documents, force_pdf=bool(metadata.get("draw_every_pdf_page")),
            ) if vision_model else ([], [])
            if not self._current(identifier, generation):
                return
            if not documents and not drawings:
                raise ValueError("图片中未取得可索引的文字或图纸标注，请核对原图及识别模型")
            config = {**dataset["parser_config"], **metadata.get("parser_config", {})}
            parts = [*split_documents(documents, dataset["chunk_method"], config), *drawings]
            if not parts:
                raise ValueError("没有可保存的文字切片")
            embedding_id, embedding = self.models.embedding(dataset["embedding_model"])
            vectors = []
            for start in range(0, len(parts), 32):
                if not self._current(identifier, generation):
                    return
                vectors.extend(embedding.embed_documents([part.page_content for part in parts[start:start + 32]]))
            if len(vectors) != len(parts) or not vectors or any(not vector or len(vector) != len(vectors[0]) or not all(math.isfinite(float(value)) for value in vector) for vector in vectors):
                raise ValueError("嵌入模型返回了无效向量")
            paragraphs = self._review_source_rows(row, source_version, documents)
            with self.db.transaction() as connection:
                check = connection.execute("SELECT generation,run FROM documents WHERE id=?", (identifier,)).fetchone()
                if not check or check["generation"] != generation or check["run"] != "RUNNING":
                    return
                self._replace_review_source(connection, row, source_version, paragraphs)
                connection.execute("DELETE FROM chunks WHERE document_id=?", (identifier,))
                for index, (part, vector) in enumerate(zip(parts, vectors)):
                    drawing = part.metadata.get("drawing_evidence")
                    connection.execute("INSERT INTO chunks(id,document_id,dataset_id,content,metadata,vector,embedding_id,ordinal,enabled) VALUES (?,?,?,?,?,?,?,?,?)", (drawing["id"] if drawing else uid(), identifier, row["dataset_id"], part.page_content, dumps(part.metadata), dumps(vector), embedding_id, index, int(bool(part.metadata.get("source_available", True)))))
                meta = metadata
                meta.pop("drawing_warnings", None)
                if warnings:
                    meta["drawing_warnings"] = warnings[:50]
                if meta.get("ocr_recovery", {}).get("applied"):
                    meta["text_method"] = "本机 OCR 补充（待人工核对） / LangChain 分块"
                elif drawings:
                    meta["text_method"] = "图纸标注识别 / 文本索引"
                elif any(part.metadata.get("source_kind") == TRANSCRIPTION_KIND for part in parts):
                    meta["text_method"] = "图片文字转录（待核对） / LangChain 分块"
                else:
                    meta["text_method"] = "迁移解析文字 / LangChain 分块" if meta.get("imported_text_cache") else "本地解析 / LangChain 分块"
                connection.execute("UPDATE documents SET run='DONE',chunk_count=?,error='',metadata=?,updated_at=? WHERE id=?", (len(parts), dumps(meta), now(), identifier))
        except Exception as error:
            message = str(error)[:500] if isinstance(error, ValueError) else provider_error(error)
            self.db.execute("UPDATE documents SET run='FAIL',error=?,updated_at=? WHERE id=? AND generation=? AND run='RUNNING'", (message, now(), identifier, generation))

    def chunk_view(self, row):
        metadata = json.loads(row["metadata"])
        document = self.document(row["document_id"])
        return {"id": row["id"], "document_id": row["document_id"], "dataset_id": row["dataset_id"],
            "document_name": document["name"], "doc_name": document["name"], "content": row["content"],
            "content_with_weight": row["content"], "available": bool(row["enabled"]),
            "positions": [], "page_number": metadata.get("page_number"), "metadata": metadata}

    def drawing_records(self, dataset_id, document_id, page, page_size):
        self.document(document_id, dataset_id)
        predicate = "document_id=? AND json_type(metadata,'$.drawing_evidence')='object'"
        total = self.db.one(f"SELECT count(*) AS total FROM chunks WHERE {predicate}", (document_id,))["total"]
        rows = self.db.rows(f"SELECT metadata FROM chunks WHERE {predicate} ORDER BY ordinal,id LIMIT ? OFFSET ?",
                            (document_id, page_size, (page - 1) * page_size))
        return {"rows": [json.loads(row["metadata"])["drawing_evidence"] for row in rows], "total": total}

    def drawing_record(self, dataset_id, document_id, chunk_id):
        self.document(document_id, dataset_id)
        row = self.db.one("SELECT metadata FROM chunks WHERE id=? AND document_id=?", (chunk_id, document_id))
        evidence = json.loads(row["metadata"]).get("drawing_evidence") if row else None
        if not evidence:
            raise HTTPException(404, "图纸标注不存在，可能已重新解析")
        return evidence

    def drawing_image(self, dataset_id, document_id, chunk_id):
        row = self.document(document_id, dataset_id)
        evidence = self.drawing_record(dataset_id, document_id, chunk_id)
        source = self.file_path(row)
        with source.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != evidence.get("source_sha256"):
                raise HTTPException(409, "原图已经改变，请重新解析文件")
        if not evidence.get("source_part"):
            raise HTTPException(400, "该证据请通过原文件预览")
        with closing(drawing_pages(source, text_pages=set(), source_part=evidence["source_part"])) as pages:
            for page in pages:
                with page.image, BytesIO() as output:
                    page.image.save(output, "PNG")
                    return output.getvalue()
        raise HTTPException(404, "内嵌原图不存在或无法解码")

    def review_drawing(self, dataset_id, document_id, chunk_id, update: DrawingReviewInput):
        changes = update.model_dump(exclude_unset=True)
        for name in ("annotation_box", "object_box"):
            box = changes.get(name)
            if box is not None and (box[0] >= box[2] or box[1] >= box[3]):
                raise HTTPException(400, "标注区域坐标无效")
        updated = self.save_chunk(dataset_id, document_id, {}, chunk_id, drawing_changes=changes)
        return updated["metadata"]["drawing_evidence"]

    def save_chunk(self, dataset_id, document_id, body, chunk_id=None, *, drawing_changes=None):
        document = self.document(document_id, dataset_id)
        if document["run"] == "RUNNING":
            raise HTTPException(409, "请先等待解析完成或取消解析")
        existing = self.db.one("SELECT * FROM chunks WHERE id=? AND document_id=?", (chunk_id, document_id)) if chunk_id else None
        if chunk_id and not existing:
            raise HTTPException(404, "切片不存在")
        metadata = json.loads(existing["metadata"]) if existing else {}
        drawing = metadata.get("drawing_evidence")
        if drawing_changes is not None:
            if not drawing:
                raise HTTPException(404, "图纸标注不存在，可能已重新解析")
            if drawing_changes["version"] != drawing["version"] or document["run"] != "DONE":
                raise HTTPException(409, "标注或解析状态已改变，请刷新后重试")
            with self.file_path(document).open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != drawing["source_sha256"]:
                    raise HTTPException(409, "原图已经改变，请重新解析文件")
            drawing.update(drawing_changes)
            if drawing["status"] == "confirmed" and not (drawing["annotation_box"] and drawing["object_box"]):
                raise HTTPException(400, "确认前请标出文字标注和对应对象的两个区域")
            drawing["version"] += 1
            content = drawing_text(drawing)
            enabled = drawing["status"] != "rejected"
            metadata["source_available"] = enabled
        else:
            if drawing:
                raise HTTPException(400, "请在图纸标注表中核对或修改该切片")
            content = str(body.get("content", existing["content"] if existing else "")).strip()
            enabled = body.get("available", existing["enabled"] if existing else True)
        if not content or len(content) > 100000:
            raise HTTPException(400, "切片内容不能为空，且不得超过十万字符")
        if drawing and content == existing["content"]:
            embedding_id, vector = existing["embedding_id"], json.loads(existing["vector"])
        else:
            embedding_id, embedding = self.models.embedding(self.dataset(dataset_id)["embedding_model"])
            vector = embedding.embed_query(content)
        try:
            valid = isinstance(vector, (list, tuple)) and bool(vector) and all(not isinstance(value, (bool, list, dict, tuple)) and math.isfinite(float(value)) for value in vector)
        except (TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            raise HTTPException(400, "嵌入模型返回了无效向量，切片未保存；请检查模型接入后重试")
        vector = [float(value) for value in vector]
        identifier = chunk_id or uid()
        with self.db.transaction() as connection:
            current = connection.execute("SELECT * FROM documents WHERE id=? AND dataset_id=?", (document_id, dataset_id)).fetchone()
            if not current or any(current[key] != document[key] for key in ("generation", "run", "updated_at")):
                raise HTTPException(409, "文件或解析状态已改变，切片未保存；请刷新后重试")
            if existing:
                current_chunk = connection.execute("SELECT * FROM chunks WHERE id=?", (chunk_id,)).fetchone()
                if not current_chunk or dict(current_chunk) != existing:
                    raise HTTPException(409, "该切片已被其他操作修改，请刷新后重试")
            if connection.execute("SELECT id FROM chunks WHERE document_id=? AND embedding_id<>? LIMIT 1", (document_id, embedding_id)).fetchone():
                raise HTTPException(409, "文件仍有旧嵌入模型的索引，请先重新解析整个文件，再编辑切片")
            reference = connection.execute("SELECT vector FROM chunks WHERE dataset_id=? AND embedding_id=? LIMIT 1", (dataset_id, embedding_id)).fetchone()
            if reference and len(json.loads(reference["vector"])) != len(vector):
                raise HTTPException(409, "模型返回的向量维度与现有索引不一致，请核对嵌入模型并重新解析知识库")
            ordinal = existing["ordinal"] if existing else connection.execute("SELECT COALESCE(MAX(ordinal),-1)+1 AS n FROM chunks WHERE document_id=?", (document_id,)).fetchone()["n"]
            connection.execute("INSERT INTO chunks(id,document_id,dataset_id,content,metadata,vector,embedding_id,ordinal,enabled) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET content=excluded.content,metadata=excluded.metadata,vector=excluded.vector,embedding_id=excluded.embedding_id,enabled=excluded.enabled",
                (identifier, document_id, dataset_id, content, dumps(metadata), dumps(vector), embedding_id, ordinal, int(enabled)))
            connection.execute("UPDATE documents SET chunk_count=(SELECT COUNT(*) FROM chunks WHERE document_id=?),run='DONE',error='',updated_at=? WHERE id=?", (document_id, now(), document_id))
        return self.chunk_view(self.db.one("SELECT * FROM chunks WHERE id=?", (identifier,)))

    def delete_chunks(self, dataset_id, document_id, identifiers):
        with self.db.transaction() as connection:
            document = connection.execute("SELECT run FROM documents WHERE id=? AND dataset_id=?", (document_id, dataset_id)).fetchone()
            if not document:
                raise HTTPException(404, "文档不存在或不属于该知识库")
            if document["run"] == "RUNNING":
                raise HTTPException(409, "请先等待解析完成或取消解析，再删除切片")
            for identifier in identifiers:
                if not connection.execute("SELECT id FROM chunks WHERE id=? AND document_id=?", (identifier, document_id)).fetchone():
                    raise HTTPException(404, "部分切片已不存在，未执行本次删除；请刷新后重试")
            for identifier in identifiers:
                connection.execute("DELETE FROM chunks WHERE id=? AND document_id=?", (identifier, document_id))
            connection.execute("UPDATE documents SET chunk_count=(SELECT COUNT(*) FROM chunks WHERE document_id=?),updated_at=? WHERE id=?", (document_id, now(), document_id))

    def extracted_text(self, identifier):
        row = self.document(identifier)
        metadata = json.loads(row["metadata"])
        if row["run"] == "DONE" and metadata.get("text_method", "").startswith(("图纸标注识别", "图片文字转录")):
            chunks = self.db.rows("SELECT content FROM chunks WHERE document_id=? AND enabled=1 ORDER BY ordinal", (identifier,))
            return {"text": "\n\n".join(item["content"] for item in chunks),
                    "method": metadata.get("text_method", "图纸标注识别"), "name": row["name"]}
        documents = self.text_documents(row)
        return {"text": "\n\n".join(item.page_content for item in documents), "method": metadata.get("text_method", "本地文档解析"), "name": row["name"]}

    def preview(self, identifier):
        row = self.document(identifier)
        destination = self.settings.data_dir / "previews" / (identifier + ".pdf")
        with self.preview_lock:
            if not destination.is_file():
                documents = self.text_documents(row)
                text_preview_pdf(row["name"], documents, destination)
        return destination
