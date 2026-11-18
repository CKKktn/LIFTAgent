"""Import a verified knowledge export into an existing local workspace.

This tool never logs into a website, contacts a model, creates an administrator,
or initializes the application. Source files stay unchanged. Import mappings
make retries safe; changed source material and local edits are never overwritten.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import sys
from urllib.parse import urlsplit, urlunsplit

# Executing a script in tools/ otherwise places only that directory on sys.path.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv.parser import parse_stream

from backend.db import Database, dumps, now, uid
from backend.models import LocalFeatureEmbeddings


EMBEDDING_ID = "local-features-v1"
SOURCE_MODEL_ERROR = re.compile(
    r"\*\*ERROR\*\*:\s*Failed to connect to Ollama\. Please check that Ollama "
    r"is downloaded, running and accessible\.\s*https://ollama\.com/download",
    re.I,
)
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
SHA256 = re.compile(r"[a-fA-F0-9]{64}\Z")


class ImportValidationError(ValueError):
    """The export or target failed validation before any migration writes."""


class ExistingDatabase(Database):
    def __init__(self, path: Path):
        # Database.__init__ resets RUNNING parsing jobs. Reuse only its operation
        # methods, so importing beside a live server does not touch those jobs.
        self.path = Path(path)


def file_sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError) as error:
        raise ImportValidationError("导出文件不是有效的 UTF-8 JSON") from error


def read_verified_json(path, expected_hash):
    """Parse the same bytes whose checksum was checked, avoiding a read race."""
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_hash.lower():
        raise ImportValidationError("导出 JSON 在校验期间发生变化，未写入目标")
    try:
        return json.loads(raw.decode("utf-8-sig"),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError) as error:
        raise ImportValidationError("导出文件不是有效的 UTF-8 JSON") from error


def source_path(root: Path, relative, *, directory=False):
    if (not isinstance(relative, str) or not relative or "\\" in relative
        or PureWindowsPath(relative).drive or PurePosixPath(relative).is_absolute()
        or any(part in {".", ".."} for part in relative.split("/"))):
        raise ImportValidationError("导出清单含有不安全的文件路径")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not (path.is_dir() if directory else path.is_file()):
        raise ImportValidationError("导出文件缺失，或路径超出了导出目录")
    return path


def target_path(root: Path, relative):
    # All callers construct relative paths from UUIDs or fixed directory names.
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ImportValidationError("目标文件路径超出了应用数据目录")
    return path


def valid_identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ImportValidationError("导出清单含有无效的来源标识")
    return value


def source_url(value):
    if not isinstance(value, str):
        raise ImportValidationError("导出清单缺少来源地址")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
        or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ImportValidationError("来源地址必须为不含凭据的 http(s) 地址")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(),
        parsed.path.rstrip("/") + "/", "", ""))


def enabled(value, default=True):
    if value is None:
        return int(default)
    if value in (True, 1, "1", "true", "True"):
        return 1
    if value in (False, 0, "0", "false", "False"):
        return 0
    raise ImportValidationError("来源文件的启用状态无效")


def verified_file(root, document, kind):
    if document.get(kind + "_status", "ok") != "ok":
        raise ImportValidationError("存在尚未成功导出的原件或切片，不能迁移")
    expected = document.get(kind + "_sha256")
    if not isinstance(expected, str) or not SHA256.fullmatch(expected):
        raise ImportValidationError("导出清单缺少有效的 SHA-256 校验值")
    path = source_path(root, document.get(kind + "_path"))
    if file_sha256(path) != expected.lower():
        raise ImportValidationError("导出文件 SHA-256 校验失败，未写入目标")
    return path


@dataclass
class SourceDocument:
    source_id: str
    name: str
    original_path: Path
    extension: str
    size: int
    enabled: int
    fingerprint: str
    hashes: dict
    chunks: list
    cache: list
    source_metadata: dict
    audit: dict
    recovery: dict | None = None


@dataclass
class SourceDataset:
    source_id: str
    name: str
    metadata: dict
    documents: list[SourceDocument]


def prepare_export(export_dir):
    """Validate *all* input before changing the target, including skipped docs."""
    root = Path(export_dir).resolve()
    if not root.is_dir():
        raise ImportValidationError("导出目录不存在")
    manifest = read_json(source_path(root, "export-manifest.json"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("datasets"), list):
        raise ImportValidationError("导出清单格式无效")
    origin = source_url(manifest.get("source"))
    result, seen_datasets = [], set()
    for dataset in manifest["datasets"]:
        if not isinstance(dataset, dict):
            raise ImportValidationError("知识库清单格式无效")
        dataset_id = valid_identifier(dataset.get("id"))
        if dataset_id in seen_datasets:
            raise ImportValidationError("导出清单包含重复的知识库标识")
        seen_datasets.add(dataset_id)
        directory = source_path(root, dataset.get("directory"), directory=True)
        metadata = read_json(source_path(root, directory.relative_to(root).as_posix() + "/dataset.json"))
        source_documents = read_json(source_path(root, directory.relative_to(root).as_posix() + "/documents.json"))
        if (not isinstance(metadata, dict) or metadata.get("id") != dataset_id
            or not isinstance(source_documents, list) or not isinstance(dataset.get("documents"), list)):
            raise ImportValidationError("知识库或文档元数据与导出清单不符")
        inventory = {}
        for item in source_documents:
            if not isinstance(item, dict):
                raise ImportValidationError("源文档元数据格式无效")
            key = valid_identifier(item.get("id"))
            if key in inventory or item.get("dataset_id", dataset_id) != dataset_id:
                raise ImportValidationError("源文档清单有重复标识或跨知识库引用")
            inventory[key] = item
        documents, seen_documents = [], set()
        for item in dataset["documents"]:
            if not isinstance(item, dict):
                raise ImportValidationError("文档导出清单格式无效")
            key = valid_identifier(item.get("id"))
            if key in seen_documents or key not in inventory:
                raise ImportValidationError("文档清单含有重复标识，或缺少原站元数据")
            seen_documents.add(key)
            original = verified_file(root, item, "original")
            chunks_path = verified_file(root, item, "chunks")
            verified_file(root, item, "text")
            if item.get("original_bytes") != original.stat().st_size:
                raise ImportValidationError("原件长度与导出清单不符")
            raw_chunks = read_verified_json(chunks_path, item["chunks_sha256"])
            if not isinstance(raw_chunks, list) or len(raw_chunks) != item.get("chunk_count"):
                raise ImportValidationError("源切片数量与导出清单不符")
            name = item.get("name")
            if not isinstance(name, str) or not name.strip() or "\0" in name:
                raise ImportValidationError("源文件名无效")
            extension = original.suffix.lower()
            if extension and not re.fullmatch(r"\.[a-z0-9]{1,16}", extension):
                raise ImportValidationError("原件扩展名不安全")
            chunks, cache, chunk_ids = [], [], set()
            audit = {"raw_source_chunk_count": len(raw_chunks), "cleaned_count": 0,
                "empty_filtered": 0, "model_error_segments_removed": 0}
            for ordinal, chunk in enumerate(raw_chunks):
                if not isinstance(chunk, dict):
                    raise ImportValidationError("源切片格式无效")
                if (chunk.get("dataset_id", dataset_id) != dataset_id
                    or chunk.get("document_id", key) != key):
                    raise ImportValidationError("源切片含有跨文档引用")
                chunk_id = chunk.get("id", str(ordinal))
                if not isinstance(chunk_id, str) or not chunk_id or len(chunk_id) > 256 or chunk_id in chunk_ids:
                    raise ImportValidationError("源切片标识无效或重复")
                chunk_ids.add(chunk_id)
                raw_content = chunk.get("content", chunk.get("content_with_weight", ""))
                if not isinstance(raw_content, str):
                    raise ImportValidationError("源切片正文必须是文字")
                content, removed = SOURCE_MODEL_ERROR.subn("", raw_content)
                content = content.replace("\0", "").strip()
                audit["model_error_segments_removed"] += removed
                audit["cleaned_count"] += int(removed > 0)
                audit["empty_filtered"] += int(not content)
                extra = chunk.get("metadata", {})
                if not isinstance(extra, dict):
                    raise ImportValidationError("源切片附加元数据无效")
                meta = {**extra, **{k: v for k, v in chunk.items()
                    if k not in {"content", "content_with_weight", "metadata", "id", "dataset_id", "document_id"}},
                    "source_chunk_id": chunk_id, "source_ordinal": ordinal,
                    "import_source": {"url": origin, "dataset_id": dataset_id, "document_id": key, "chunk_id": chunk_id},
                    "source_available": bool(enabled(chunk.get("available", True))),
                    "source_kind": extra.get("source_kind", "imported-text")}
                positions = meta.get("positions")
                if "page_number" not in meta and isinstance(positions, list) and positions:
                    page = positions[0][0] if isinstance(positions[0], list) and positions[0] else None
                    if isinstance(page, int) and not isinstance(page, bool) and page > 0:
                        meta["page_number"] = page
                # Keep the original content for audit, including the exact source
                # model error. Only cleaned page_content is eligible for indexing.
                cache_meta = {**meta, "raw_source_content": raw_content}
                cache.append({"page_content": content, "metadata": cache_meta})
                if content:
                    chunks.append({"content": content, "metadata": meta,
                        "enabled": enabled(chunk.get("available", True)), "ordinal": ordinal})
            hashes = {kind: item[kind + "_sha256"].lower() for kind in ("original", "chunks", "text")}
            source_metadata = inventory[key]
            fingerprint = json_fingerprint({"hashes": hashes, "name": name,
                "version": item.get("source_version"), "document": source_metadata,
                "dataset_status": metadata.get("status", "1")})
            documents.append(SourceDocument(key, name, original, extension, original.stat().st_size,
                enabled(source_metadata.get("status", "1")) * enabled(metadata.get("status", "1")),
                fingerprint, hashes, chunks, cache, source_metadata, audit))
        if seen_documents != set(inventory):
            raise ImportValidationError("导出清单未覆盖全部原站文档")
        if dataset.get("source_document_count", len(documents)) != len(documents):
            raise ImportValidationError("导出文档数量与原站清单不符")
        raw_chunk_count = sum(document.audit["raw_source_chunk_count"] for document in documents)
        if dataset.get("source_chunk_count", raw_chunk_count) != raw_chunk_count:
            raise ImportValidationError("导出切片总数与原站清单不符")
        name = dataset.get("name", metadata.get("name", dataset_id))
        if not isinstance(name, str) or not name.strip() or "\0" in name:
            raise ImportValidationError("源知识库名称无效")
        result.append(SourceDataset(dataset_id, name, metadata, documents))
    return origin, result


def apply_ocr_recovery(export_dir, recovery_path, datasets, origin=None):
    """Validate local OCR artifacts and enrich only documents lacking good text.

    OCR itself runs separately. Consuming its saved output here never loads an
    OCR provider, sends documents over the network, or treats OCR as verified.
    Artifact paths in its manifest are relative to the original export root.
    """
    root = Path(export_dir).resolve()
    origin = origin or source_url(read_json(source_path(root, "export-manifest.json")).get("source"))
    manifest_path = Path(recovery_path).resolve()
    if manifest_path.is_dir():
        manifest_path = (manifest_path / "manifest.json").resolve()
    if not manifest_path.is_relative_to(root) or not manifest_path.is_file():
        raise ImportValidationError("本机 OCR 清单必须位于原导出目录内")
    manifest = read_json(manifest_path)
    if (not isinstance(manifest, dict) or manifest.get("version") != 1
        or not isinstance(manifest.get("engine"), str) or not manifest["engine"].strip()
        or not isinstance(manifest.get("documents"), list)):
        raise ImportValidationError("本机 OCR 清单格式无效")
    documents = {(kb.source_id, doc.source_id): doc for kb in datasets for doc in kb.documents}
    seen, prepared = set(), []
    for record in manifest["documents"]:
        if not isinstance(record, dict):
            raise ImportValidationError("本机 OCR 文档记录格式无效")
        key = (valid_identifier(record.get("dataset_id")), valid_identifier(record.get("document_id")))
        if key in seen or key not in documents:
            raise ImportValidationError("本机 OCR 清单有重复或未知的源文档")
        seen.add(key)
        document = documents[key]
        if record.get("source_original_sha256") != document.hashes["original"]:
            raise ImportValidationError("本机 OCR 对应的原件 SHA-256 与导出清单不符")
        if record.get("status") not in {"recovered", "no_text"}:
            raise ImportValidationError("本机 OCR 记录状态无效")
        if (record.get("reason") is not None and not isinstance(record["reason"], str)
            or not isinstance(record.get("warnings", []), list)
            or any(not isinstance(item, str) for item in record.get("warnings", []))):
            raise ImportValidationError("本机 OCR 质量提示格式无效")
        expected = record.get("entries_sha256")
        if not isinstance(expected, str) or not SHA256.fullmatch(expected):
            raise ImportValidationError("本机 OCR 记录缺少有效的 SHA-256")
        path = source_path(root, record.get("entries_path"))
        entries = read_verified_json(path, expected)
        if not isinstance(entries, list):
            raise ImportValidationError("本机 OCR 正文必须是列表")
        for entry in entries:
            if (not isinstance(entry, dict) or not isinstance(entry.get("page_content"), str)
                or not isinstance(entry.get("metadata"), dict)
                or entry["metadata"].get("requires_human_verification") is not True):
                raise ImportValidationError("本机 OCR 正文格式无效，或缺少人工核对标记")
        characters = sum(len(entry["page_content"]) for entry in entries)
        has_text = any(entry["page_content"].replace("\0", "").strip() for entry in entries)
        if (record.get("content_characters") != characters
            or (record["status"] == "recovered") != has_text):
            raise ImportValidationError("本机 OCR 正文长度或状态与清单不符")
        prepared.append((document, record, entries))
    # Mutate the in-memory plan only after the entire OCR manifest is checked.
    for document, record, entries in prepared:
        recovery = {"engine": manifest["engine"], "quality": "本机OCR，待人工核对",
            "requires_human_verification": True, "source_original_sha256": record["source_original_sha256"],
            "entries_path": record["entries_path"], "entries_sha256": record["entries_sha256"],
            "status": record["status"], "content_characters": record["content_characters"],
            "reason": record.get("reason"), "warnings": record.get("warnings", []),
            "applied": False, "entries_indexed": 0}
        # Source text already judged usable stays authoritative. OCR artifacts
        # remain in the export, but do not silently add alternative body text.
        if document.chunks or record["status"] == "no_text":
            document.recovery = recovery
            continue
        for index, entry in enumerate(entries):
            raw_content = entry["page_content"]
            content = raw_content.replace("\0", "").strip()
            if not content:
                continue
            ordinal = document.audit["raw_source_chunk_count"] + index
            meta = {**entry["metadata"], "source_chunk_id": "recovered-ocr-" + str(index),
                "source_ordinal": ordinal, "source_available": True,
                "ocr_recovery_entries_path": record["entries_path"],
                "ocr_recovery_entries_sha256": record["entries_sha256"],
                "quality": "本机OCR，待人工核对", "requires_human_verification": True,
                "import_source": {"url": origin, "dataset_id": record["dataset_id"],
                    "document_id": record["document_id"], "ocr_entry": index}}
            document.cache.append({"page_content": content, "metadata": {**meta, "raw_source_content": raw_content}})
            document.chunks.append({"content": content, "metadata": meta, "enabled": 1, "ordinal": ordinal})
            recovery["entries_indexed"] += 1
        recovery["applied"] = bool(recovery["entries_indexed"])
        document.recovery = recovery
        document.audit["ocr_recovered_entries"] = recovery["entries_indexed"]
        document.audit["ocr_recovered_characters"] = recovery["content_characters"]
        document.fingerprint = json_fingerprint({"source_fingerprint": document.fingerprint, "ocr_recovery": recovery})


def resolve_target(app_dir, data_dir=None):
    app_root = Path(app_dir).resolve()
    if not app_root.is_dir():
        raise ImportValidationError("目标工程目录不存在")
    configured = None
    environment = app_root / ".env"
    if data_dir is None and environment.is_file():
        # Parse the data-dir field only. Never load the file into os.environ,
        # configure a provider, or return/log other settings and API keys.
        with environment.open(encoding="utf-8-sig") as stream:
            for binding in parse_stream(stream):
                if binding.key == "APP_DATA_DIR":
                    if binding.error:
                        raise ImportValidationError("目标 .env 的 APP_DATA_DIR 格式无效")
                    configured = binding.value
        if configured and "${" in configured:
            raise ImportValidationError("APP_DATA_DIR 含变量，请用 --data-dir 明确指定实际目录")
    directory = Path(data_dir if data_dir is not None else configured or ".local-data")
    directory = (directory if directory.is_absolute() else app_root / directory).resolve()
    database = target_path(directory, "workspace.sqlite3")
    secret = target_path(directory, "session.secret")
    if not directory.is_dir() or not database.is_file() or not secret.is_file() or not secret.stat().st_size:
        raise ImportValidationError("目标工作区尚未初始化：请先启动新系统，再选择含数据库和 session.secret 的数据目录")
    expected = {
        "users": {"id", "username", "password_hash", "created_at"},
        "datasets": {"id", "payload"}, "chats": {"id", "payload"},
        "documents": {"id", "dataset_id", "name", "path", "extension", "size", "run", "enabled",
            "chunk_count", "error", "generation", "metadata", "created_at", "updated_at"},
        "chunks": {"id", "document_id", "dataset_id", "content", "metadata", "vector", "embedding_id", "ordinal", "enabled"},
    }
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            for table, fields in expected.items():
                present = {row[1] for row in connection.execute("PRAGMA table_info(" + table + ")")}
                if not fields.issubset(present):
                    raise ImportValidationError("目标数据库与新系统的数据库结构不兼容")
    except sqlite3.DatabaseError as error:
        raise ImportValidationError("无法读取目标数据库") from error
    for relative in ("uploads", "imported-text", ".knowledge-import.lock"):
        target_path(directory, relative)
    return directory, ExistingDatabase(database)


@contextmanager
def import_lock(root):
    """An OS lock is released automatically even when an import is interrupted."""
    path = target_path(root, ".knowledge-import.lock")
    with path.open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ImportValidationError("此工作区已有迁移程序运行，请等待其完成") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class KnowledgeImporter:
    def __init__(self, db, data_dir, origin, datasets, progress=None):
        self.db, self.data_dir = db, Path(data_dir).resolve()
        self.origin, self.datasets, self.progress = origin, datasets, progress
        self.embedding = LocalFeatureEmbeddings()
        self._created_files = set()
        self.statistics = {"source_datasets": len(datasets),
            "source_documents": sum(len(kb.documents) for kb in datasets),
            "raw_source_chunks": sum(doc.audit["raw_source_chunk_count"] for kb in datasets for doc in kb.documents),
            "effective_source_chunks": sum(doc.audit["raw_source_chunk_count"] - doc.audit["empty_filtered"] for kb in datasets for doc in kb.documents),
            "index_chunks_total": sum(len(doc.chunks) for kb in datasets for doc in kb.documents),
            "cleaned_count": sum(doc.audit["cleaned_count"] for kb in datasets for doc in kb.documents),
            "empty_filtered": sum(doc.audit["empty_filtered"] for kb in datasets for doc in kb.documents),
            "model_error_segments_removed": sum(doc.audit["model_error_segments_removed"] for kb in datasets for doc in kb.documents),
            "documents_without_text": sum(not doc.chunks for kb in datasets for doc in kb.documents),
            "ocr_records": sum(doc.recovery is not None for kb in datasets for doc in kb.documents),
            "ocr_recovered_documents": sum(bool(doc.recovery and doc.recovery["applied"]) for kb in datasets for doc in kb.documents),
            "ocr_recovered_entries": sum(doc.recovery["entries_indexed"] if doc.recovery else 0 for kb in datasets for doc in kb.documents),
            "ocr_recovered_characters": sum(doc.recovery["content_characters"] if doc.recovery and doc.recovery["applied"] else 0 for kb in datasets for doc in kb.documents),
            "ocr_no_text_documents": sum(bool(doc.recovery and doc.recovery["status"] == "no_text") for kb in datasets for doc in kb.documents),
            "datasets_created": 0, "datasets_reused": 0, "assistants_created": 0,
            "documents_imported": 0, "documents_reused": 0, "chunks_imported": 0,
            "locally_modified": 0, "recovered_incomplete_files": 0, "conflicts": [], "failures": [], "datasets": []}

    def _schema(self):
        # No foreign-key cascade: mappings retain deliberate user deletions, so
        # a retry cannot unexpectedly resurrect deleted documents or knowledge.
        with self.db.transaction() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS knowledge_imports (
                source_url TEXT NOT NULL, source_dataset_id TEXT NOT NULL,
                source_document_id TEXT NOT NULL DEFAULT '', local_dataset_id TEXT NOT NULL,
                local_document_id TEXT NOT NULL DEFAULT '', local_chat_id TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL, target_fingerprint TEXT NOT NULL DEFAULT '',
                original_sha256 TEXT NOT NULL DEFAULT '', chunks_sha256 TEXT NOT NULL DEFAULT '',
                text_sha256 TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                PRIMARY KEY(source_url,source_dataset_id,source_document_id))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS knowledge_import_pending (
                local_document_id TEXT PRIMARY KEY, upload_path TEXT NOT NULL,
                cache_path TEXT NOT NULL, original_sha256 TEXT NOT NULL,
                cache_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)""")

    def _recover_pending(self):
        for pending in self.db.rows("SELECT * FROM knowledge_import_pending"):
            identifier = pending["local_document_id"]
            if not re.fullmatch(r"[a-f0-9]{32}", identifier):
                raise ImportValidationError("上次迁移的恢复记录无效，请检查数据目录")
            # If a commit completed, the files now belong to the real document.
            if not self.db.one("SELECT id FROM documents WHERE id=? OR path=?",
                (identifier, Path(pending["upload_path"]).name)):
                for field in ("upload_path", "cache_path"):
                    relative = pending[field]
                    if ((field == "upload_path" and not re.fullmatch(r"uploads/" + identifier + r"(?:\.[a-z0-9]{1,16})?", relative))
                        or (field == "cache_path" and relative != "imported-text/" + identifier + ".json")):
                        raise ImportValidationError("上次迁移的恢复路径无效")
                    path = target_path(self.data_dir, relative)
                    # A partial copy may not match its final hash. Its UUID was
                    # reserved before creating the exclusive file, and no live
                    # document references it. These are importer-owned files.
                    if path.is_file():
                        path.unlink()
                        self.statistics["recovered_incomplete_files"] += 1
            self.db.execute("DELETE FROM knowledge_import_pending WHERE local_document_id=?", (identifier,))

    def _mapping(self, dataset_id, document_id=""):
        return self.db.one("SELECT * FROM knowledge_imports WHERE source_url=? AND source_dataset_id=? AND source_document_id=?",
            (self.origin, dataset_id, document_id))

    def _conflict(self, reason, dataset_id, document_id=""):
        self.statistics["conflicts"].append({"reason": reason, "source_dataset_id": dataset_id,
            **({"source_document_id": document_id} if document_id else {})})

    def _dataset(self, dataset):
        with self.db.transaction() as connection:
            mapped = connection.execute("SELECT * FROM knowledge_imports WHERE source_url=? AND source_dataset_id=? AND source_document_id=''",
                (self.origin, dataset.source_id)).fetchone()
            if mapped:
                if not connection.execute("SELECT id FROM datasets WHERE id=?", (mapped["local_dataset_id"],)).fetchone():
                    self._conflict("local_dataset_deleted", dataset.source_id)
                    return None
                self.statistics["datasets_reused"] += 1
                return mapped["local_dataset_id"]
            names = {json.loads(row[0]).get("name") for row in connection.execute("SELECT payload FROM datasets")}
            name, sequence = dataset.name[:200], 1
            while name in names:
                suffix = "（迁移）" if sequence == 1 else "（迁移 " + str(sequence) + "）"
                name = dataset.name[:200 - len(suffix)] + suffix
                sequence += 1
            identifier, chat_id, timestamp = uid(), uid(), now()
            provenance = {"url": self.origin, "dataset_id": dataset.source_id}
            has_ocr = any(doc.recovery and doc.recovery["applied"] for doc in dataset.documents)
            value = {"id": identifier, "name": name,
                "description": "从已有知识库迁移；保留原件及原站解析文字。"
                    + ("部分缺失正文由本机 OCR 补充，待人工核对。" if has_ocr else "")
                    + "本地文本特征索引，无需模型密钥。",
                "chunk_method": "naive", "embedding_model": "local-features",
                "parser_config": {"chunk_size": 1000, "chunk_overlap": 120},
                "pipeline_id": "", "create_time": timestamp, "update_time": timestamp,
                "import_source": provenance, "source_name": dataset.name,
                "source_chunk_method": dataset.metadata.get("chunk_method"),
                "source_status": dataset.metadata.get("status", "1")}
            assistant = {"id": chat_id, "name": (name[:192] + " · 协审"), "dataset_ids": [identifier],
                "llm_id": "", "similarity_threshold": .15, "create_time": timestamp,
                "update_time": timestamp, "import_source": provenance}
            connection.execute("INSERT INTO datasets(id,payload) VALUES (?,?)", (identifier, dumps(value)))
            connection.execute("INSERT INTO chats(id,payload) VALUES (?,?)", (chat_id, dumps(assistant)))
            connection.execute("""INSERT INTO knowledge_imports(source_url,source_dataset_id,source_document_id,
                local_dataset_id,local_chat_id,fingerprint,created_at) VALUES (?,?,?,?,?,?,?)""",
                (self.origin, dataset.source_id, "", identifier, chat_id,
                    json_fingerprint(dataset.metadata), timestamp))
            self.statistics["datasets_created"] += 1
            self.statistics["assistants_created"] += 1
            return identifier

    def _target_fingerprint(self, connection, document_id):
        row = connection.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if not row:
            return ""
        document = dict(row)
        chunks = [dict(item) for item in connection.execute("SELECT * FROM chunks WHERE document_id=? ORDER BY ordinal,id", (document_id,))]
        files = {}
        for label, relative in (("original", "uploads/" + document["path"]),
            ("cache", "imported-text/" + document_id + ".json")):
            path = target_path(self.data_dir, relative)
            files[label] = file_sha256(path) if path.is_file() else "missing"
        return json_fingerprint({"document": document, "chunks": chunks, "files": files})

    def _reuse(self, dataset, document, local_dataset):
        mapped = self._mapping(dataset.source_id, document.source_id)
        if not mapped:
            return False
        if mapped["fingerprint"] != document.fingerprint:
            self._conflict("source_changed", dataset.source_id, document.source_id)
            return True
        with self.db.connect() as connection:
            existing = connection.execute("SELECT dataset_id FROM documents WHERE id=?", (mapped["local_document_id"],)).fetchone()
            if not existing:
                self._conflict("local_document_deleted", dataset.source_id, document.source_id)
                return True
            if existing["dataset_id"] != local_dataset:
                self._conflict("local_document_moved", dataset.source_id, document.source_id)
                return True
            if self._target_fingerprint(connection, mapped["local_document_id"]) != mapped["target_fingerprint"]:
                self.statistics["locally_modified"] += 1
        self.statistics["documents_reused"] += 1
        return True

    def _copy_original(self, document, destination):
        digest = hashlib.sha256()
        with document.original_path.open("rb") as source, destination.open("xb") as output:
            self._created_files.add(destination)
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != document.hashes["original"]:
            raise ImportValidationError("源原件在校验后发生变化，未提交此文档")

    def _import_document(self, dataset, document, local_dataset):
        if self._reuse(dataset, document, local_dataset):
            return
        identifier = uid()
        upload_relative = "uploads/" + identifier + document.extension
        cache_relative = "imported-text/" + identifier + ".json"
        upload = target_path(self.data_dir, upload_relative)
        cache = target_path(self.data_dir, cache_relative)
        if upload.exists() or cache.exists() or self.db.one("SELECT id FROM documents WHERE id=?", (identifier,)):
            raise ImportValidationError("生成的目标标识已存在，请重新执行迁移")
        cache_bytes = dumps(document.cache).encode("utf-8")
        cache_hash = hashlib.sha256(cache_bytes).hexdigest()
        timestamp = now()
        self.db.execute("""INSERT INTO knowledge_import_pending(local_document_id,upload_path,cache_path,
            original_sha256,cache_sha256,created_at) VALUES (?,?,?,?,?,?)""",
            (identifier, upload_relative, cache_relative, document.hashes["original"], cache_hash, timestamp))
        committed = False
        try:
            self._copy_original(document, upload)
            with cache.open("xb") as output:
                self._created_files.add(cache)
                output.write(cache_bytes)
                output.flush()
                os.fsync(output.fileno())
            vectors = []
            for start in range(0, len(document.chunks), 32):
                vectors.extend(self.embedding.embed_documents([chunk["content"] for chunk in document.chunks[start:start + 32]]))
            if len(vectors) != len(document.chunks) or any(len(vector) != 384 or not all(math.isfinite(value) for value in vector) for vector in vectors):
                raise ValueError("本机文本特征向量无效")
            provenance = {"url": self.origin, "dataset_id": dataset.source_id, "document_id": document.source_id}
            metadata = {"imported_text_cache": True, "imported_text_sha256": cache_hash,
                "text_method": "原站解析文字 / 本机 OCR 补充（待人工核对）" if document.recovery and document.recovery["applied"] else "原站已有解析文字（迁移）",
                "import_source": provenance,
                "source_name": document.name, "source_status": document.source_metadata.get("status", "1"),
                "source_run": document.source_metadata.get("run"), "source_hashes": document.hashes,
                "source_has_replacement_characters": "\ufffd" in document.name,
                "import_audit": document.audit}
            if document.recovery:
                metadata["ocr_recovery"] = document.recovery
            with self.db.transaction() as connection:
                connection.execute("""INSERT INTO documents(id,dataset_id,name,path,extension,size,run,enabled,
                    chunk_count,error,metadata,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (identifier, local_dataset, document.name, upload.name, document.extension, document.size,
                        "DONE" if document.chunks else "UNSTART", document.enabled, len(document.chunks),
                        "" if document.chunks else "原件已迁移，但原站没有有效解析文字；需补充 OCR 或文字提取后才能检索正文",
                        dumps(metadata), timestamp, timestamp))
                for chunk, vector in zip(document.chunks, vectors):
                    connection.execute("""INSERT INTO chunks(id,document_id,dataset_id,content,metadata,vector,
                        embedding_id,ordinal,enabled) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (uid(), identifier, local_dataset, chunk["content"], dumps(chunk["metadata"]),
                            dumps(vector), EMBEDDING_ID, chunk["ordinal"], chunk["enabled"]))
                fingerprint = self._target_fingerprint(connection, identifier)
                connection.execute("""INSERT INTO knowledge_imports(source_url,source_dataset_id,source_document_id,
                    local_dataset_id,local_document_id,fingerprint,target_fingerprint,original_sha256,chunks_sha256,
                    text_sha256,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (self.origin, dataset.source_id, document.source_id, local_dataset, identifier,
                        document.fingerprint, fingerprint, document.hashes["original"], document.hashes["chunks"],
                        document.hashes["text"], timestamp))
                connection.execute("DELETE FROM knowledge_import_pending WHERE local_document_id=?", (identifier,))
            committed = True
            self.statistics["documents_imported"] += 1
            self.statistics["chunks_imported"] += len(document.chunks)
        finally:
            if not committed:
                # Only these newly reserved UUID paths can be removed. Existing
                # documents, administrator data, and other uploads are untouched.
                # A commit may have succeeded just before an interruption. Check
                # the authoritative row before removing any files in that case.
                persisted = self.db.one("SELECT id FROM documents WHERE id=? OR path=?", (identifier, upload.name))
                if not persisted:
                    for path in (upload, cache):
                        if path in self._created_files:
                            path.unlink(missing_ok=True)
                self.db.execute("DELETE FROM knowledge_import_pending WHERE local_document_id=?", (identifier,))
            self._created_files.discard(upload)
            self._created_files.discard(cache)

    def run(self):
        with import_lock(self.data_dir):
            self._schema()
            self._recover_pending()
            for part in ("uploads", "imported-text"):
                target_path(self.data_dir, part).mkdir(parents=True, exist_ok=True)
            completed = 0
            for dataset in self.datasets:
                local_dataset = self._dataset(dataset)
                if not local_dataset:
                    continue
                self.statistics["datasets"].append({"source_dataset_id": dataset.source_id, "local_dataset_id": local_dataset})
                for document in dataset.documents:
                    try:
                        self._import_document(dataset, document, local_dataset)
                    except Exception as error:
                        # Exception messages from filesystems and providers may
                        # contain document names or settings. Log only the type.
                        self.statistics["failures"].append({"source_dataset_id": dataset.source_id,
                            "source_document_id": document.source_id, "error_type": type(error).__name__})
                    completed += 1
                    if self.progress:
                        self.progress(completed, self.statistics["source_documents"])
        return self.statistics


def import_export(export_dir, app_dir, data_dir=None, *, ocr_recovery=None, dry_run=False, progress=None):
    origin, datasets = prepare_export(export_dir)
    if ocr_recovery is not None:
        apply_ocr_recovery(export_dir, ocr_recovery, datasets, origin)
    directory, db = resolve_target(app_dir, data_dir)
    importer = KnowledgeImporter(db, directory, origin, datasets, progress)
    if dry_run:
        return {**importer.statistics, "dry_run": True}
    return importer.run()


def main(argv=None):
    parser = argparse.ArgumentParser(description="把已导出的知识库迁移到已初始化的新系统；只用本机特征向量，不调用模型服务")
    parser.add_argument("--export-dir", required=True, type=Path)
    parser.add_argument("--app-dir", required=True, type=Path, help="目标新系统工程目录")
    parser.add_argument("--data-dir", type=Path, help="覆盖目标 .env 中的 APP_DATA_DIR；相对路径按目标工程目录解析")
    parser.add_argument("--ocr-recovery", type=Path, help="本机 OCR 补充清单或含 manifest.json 的目录；仅补充缺少有效正文的文件")
    parser.add_argument("--dry-run", action="store_true", help="校验全部源文件和目标结构，不写入任何数据")
    args = parser.parse_args(argv)
    try:
        result = import_export(args.export_dir, args.app_dir, args.data_dir, ocr_recovery=args.ocr_recovery, dry_run=args.dry_run,
            progress=None if args.dry_run else lambda done, total: print(f"Migrating documents: {done}/{total}", file=sys.stderr, flush=True) if done % 25 == 0 or done == total else None)
    except (ImportValidationError, OSError, sqlite3.DatabaseError) as error:
        message = str(error) if isinstance(error, ImportValidationError) else type(error).__name__
        print("Migration stopped: " + message, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Migration interrupted. Committed documents are retained; run the same command to resume.", file=sys.stderr)
        return 130
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["conflicts"] or result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
