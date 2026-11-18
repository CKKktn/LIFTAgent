from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from backend.db import Database, dumps, now
from backend.models import LocalFeatureEmbeddings
from backend.retrieval import SQLiteVectorStore
from tools import import_knowledge as migration


ERROR_TEXT = ("**ERROR**: Failed to connect to Ollama. Please check that Ollama is "
    "downloaded, running and accessible. https://ollama.com/download")


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def make_export(root, *, contaminated=False):
    """Small, entirely synthetic images/OCR. No real contract material."""
    root.mkdir()
    datasets = []
    definitions = [
        ("source-kb-1", "合同资料", [
            ("image-1", "项目/技术协议截图.png", b"synthetic-image-bytes", "1", [
                {"id": "page-1", "content": "设备续航时间不得低于8小时。", "available": True,
                    "positions": [[2, 10, 20, 30, 40]], "metadata": {"source": "协议第2页"}},
                {"id": "page-2", "content": "应提交设备测试报告。", "available": False, "positions": [[3, 0, 0, 0, 0]]},
            ]),
            ("disabled-1", "停用文件.txt", b"disabled-source", "0", [
                {"id": "disabled-page", "content": "停用文件的秘密参数为9000。", "available": True},
            ]),
        ]),
        ("source-kb-2", "合同资料", [
            ("image-2", "测量记录.jpg", b"another-synthetic-image", "1", [
                {"id": "record", "content": "设备测量的续航时间为10小时。", "available": True},
            ]),
        ]),
    ]
    if contaminated:
        definitions = [("source-kb-1", "合同资料", [
            ("image-1", "技术协议.png", b"synthetic-contaminated-image", "1", [
                {"id": "mixed", "content": "合同第一句。" + ERROR_TEXT + "紧随的真实OCR正文：续航10小时。", "available": True, "positions": [[1, 1, 2, 3, 4]]},
                {"id": "error", "content": ERROR_TEXT, "available": True},
                {"id": "empty", "content": " \n ", "available": True},
                {"id": "legitimate", "content": "Error handling is a required device function.", "available": True},
            ]),
            ("image-2", "没有原OCR.png", b"synthetic-no-ocr", "1", []),
            ("image-3", "只有报错.png", b"synthetic-error-only", "1", [
                {"id": "only-error", "content": ERROR_TEXT, "available": True},
            ]),
        ])]
    for dataset_id, name, docs in definitions:
        relative = "datasets/" + dataset_id
        directory = root / relative
        metadata = {"id": dataset_id, "name": name, "status": "1", "chunk_method": "naive"}
        save_json(directory / "dataset.json", metadata)
        inventory, manifest_docs = [], []
        for document_id, document_name, original, status, chunks in docs:
            extension = Path(document_name).suffix
            original_path = directory / "originals" / (document_id + extension)
            original_path.parent.mkdir(parents=True, exist_ok=True)
            original_path.write_bytes(original)
            for chunk in chunks:
                chunk.update(dataset_id=dataset_id, document_id=document_id)
            chunks_path = directory / "chunks" / (document_id + ".json")
            save_json(chunks_path, chunks)
            text_path = directory / "text" / (document_id + ".txt")
            text_path.parent.mkdir(exist_ok=True)
            text_path.write_text("\n\n".join(c["content"] for c in chunks), encoding="utf-8")
            document = {"id": document_id, "name": document_name, "dataset_id": dataset_id,
                "status": status, "run": "DONE", "size": len(original), "chunk_count": len(chunks), "update_time": 12345}
            inventory.append(document)
            item = {"id": document_id, "name": document_name,
                "original_status": "ok", "chunks_status": "ok", "original_bytes": len(original),
                "chunk_count": len(chunks), "source_version": [len(original), len(chunks), 12345, document_name]}
            for kind, path in [("original", original_path), ("chunks", chunks_path), ("text", text_path)]:
                item[kind + "_path"] = path.relative_to(root).as_posix()
                item[kind + "_sha256"] = migration.file_sha256(path)
            manifest_docs.append(item)
        save_json(directory / "documents.json", inventory)
        datasets.append({"id": dataset_id, "name": name, "directory": relative,
            "source_document_count": len(docs), "documents": manifest_docs})
    save_json(root / "export-manifest.json", {"source": "http://source.example.test/", "datasets": datasets})
    return root


def make_target(root, *, custom_dir=False, administrator=True):
    root.mkdir()
    data = root / ("user-data" if custom_dir else ".local-data")
    data.mkdir()
    (data / "uploads").mkdir()
    (data / "session.secret").write_text("synthetic-test-secret-do-not-use-in-production", encoding="ascii")
    db = Database(data / "workspace.sqlite3")
    if administrator:
        db.execute("INSERT INTO users VALUES (?,?,?,?)", ("existing-user", "already-created-admin", "password-hash-must-stay", now()))
    db.json_save("datasets", {"id": "existing-kb", "name": "合同资料", "embedding_model": "local-features"})
    db.json_save("chats", {"id": "existing-chat", "name": "已有助手", "dataset_ids": ["existing-kb"], "llm_id": ""})
    (data / "uploads" / "existing.txt").write_text("用户之前的文件", encoding="utf-8")
    db.execute("""INSERT INTO documents(id,dataset_id,name,path,extension,size,run,generation,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)""", ("existing-doc", "existing-kb", "用户原文件.txt", "existing.txt", ".txt", 6,
            "RUNNING", 42, now(), now()))
    db.execute("INSERT INTO models VALUES (?,?,?)", ("existing-model", dumps({"id": "existing-model", "name": "keep-model"}), "encrypted-secret-must-stay"))
    db.execute("INSERT INTO model_defaults VALUES (?,?)", ("embedding", "existing-model"))
    if custom_dir:
        (root / ".env").write_text('APP_DATA_DIR="user-data"\nLLM_API_KEY="never-print-or-load-this-secret"\n', encoding="utf-8")
    return data, db


def make_ocr_recovery(export, *, include_existing_text=False):
    manifest = json.loads((export / "export-manifest.json").read_text(encoding="utf-8"))
    records = []
    for dataset in manifest["datasets"]:
        for document in dataset["documents"]:
            if document["id"] == "image-1" and not include_existing_text:
                continue
            if document["id"] == "image-3":
                entries = []
            else:
                entries = [{"page_content": "本机OCR恢复：设备温度应为零下20摄氏度至50摄氏度。", "metadata": {
                    "source_kind": "image", "page_number": 1,
                    "source_image_sha256": document["original_sha256"],
                    "ocr_engine": "Windows.Media.Ocr zh-Hans-CN", "quality": "本机OCR，待人工核对",
                    "requires_human_verification": True, "image_width": 800, "image_height": 600,
                    "ocr_lines": [{"text": "设备温度", "words": [{"text": "设备温度", "bounding_box": [10, 20, 100, 25]}]}],
                    "ocr_box_units": "image_pixels_xywh"}}]
            entries_path = export / "ocr-recovery" / (document["original_sha256"] + ".json")
            save_json(entries_path, entries)
            records.append({"dataset_id": dataset["id"], "document_id": document["id"],
                "source_original_sha256": document["original_sha256"],
                "entries_path": entries_path.relative_to(export).as_posix(),
                "entries_sha256": migration.file_sha256(entries_path),
                "content_characters": sum(len(entry["page_content"]) for entry in entries),
                "quality": "本机OCR，待人工核对", "status": "recovered" if entries else "no_text",
                "reason": None if entries else "示例原件没有可识别文字", "warnings": []})
    path = export / "ocr-recovery" / "manifest.json"
    save_json(path, {"version": 1, "engine": "Windows.Media.Ocr zh-Hans-CN", "documents": records})
    return path


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    def reject(*args, **kwargs):
        pytest.fail("Knowledge migration must not call an external model or HTTP service")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject)


def imported_document(db, source_id="image-1"):
    mapped = db.one("SELECT local_document_id FROM knowledge_imports WHERE source_document_id=?", (source_id,))
    return db.one("SELECT * FROM documents WHERE id=?", (mapped["local_document_id"],))


def test_image_ocr_import_is_searchable_without_models_and_preserves_existing_workspace(tmp_path):
    source = make_export(tmp_path / "export")
    app = tmp_path / "target"
    data, db = make_target(app)
    previous = {table: db.rows("SELECT * FROM " + table) for table in ("users", "models", "model_defaults", "documents")}
    secret = (data / "session.secret").read_bytes()
    result = migration.import_export(source, app)
    assert result["documents_imported"] == 3 and result["chunks_imported"] == 4
    assert result["datasets_created"] == 2 and result["assistants_created"] == 2
    assert not result["failures"] and not result["conflicts"]
    for table in ("users", "models", "model_defaults"):
        assert db.rows("SELECT * FROM " + table) == previous[table]
    assert db.one("SELECT * FROM documents WHERE id='existing-doc'") == previous["documents"][0]
    assert db.one("SELECT run,generation FROM documents WHERE id='existing-doc'") == {"run": "RUNNING", "generation": 42}
    assert (data / "session.secret").read_bytes() == secret
    assert (data / "uploads" / "existing.txt").read_text(encoding="utf-8") == "用户之前的文件"
    names = {item["name"] for item in db.json_list("datasets")}
    assert names == {"合同资料", "合同资料（迁移）", "合同资料（迁移 2）"}
    row = imported_document(db)
    assert row["name"] == "项目/技术协议截图.png" and row["run"] == "DONE" and row["chunk_count"] == 2
    assert row["path"] == row["id"] + ".png"
    assert (data / "uploads" / row["path"]).read_bytes() == b"synthetic-image-bytes"
    metadata = json.loads(row["metadata"])
    assert metadata["imported_text_cache"] and metadata["text_method"] == "原站已有解析文字（迁移）"
    cache_path = data / "imported-text" / (row["id"] + ".json")
    assert migration.file_sha256(cache_path) == metadata["imported_text_sha256"]
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache[0]["page_content"] == "设备续航时间不得低于8小时。"
    assert cache[0]["metadata"]["page_number"] == 2
    assert cache[0]["metadata"]["positions"] == [[2, 10, 20, 30, 40]]
    assert cache[0]["metadata"]["source"] == "协议第2页"
    assert cache[0]["metadata"]["source_chunk_id"] == "page-1"
    assert imported_document(db, "disabled-1")["enabled"] == 0
    store = SQLiteVectorStore(db, LocalFeatureEmbeddings(), migration.EMBEDDING_ID, [row["dataset_id"]])
    matches = store.similarity_search("设备续航时间", k=10)
    assert len(matches) == 1 and matches[0].metadata["document_id"] == row["id"]
    assert "8小时" in matches[0].page_content
    assert matches[0].metadata["id"] != "page-1"
    for assistant in db.json_list("chats"):
        if assistant["id"] != "existing-chat":
            assert assistant["llm_id"] == "" and len(assistant["dataset_ids"]) == 1
    assert not db.rows("SELECT * FROM knowledge_import_pending")


def test_exact_source_model_errors_are_removed_without_discarding_neighboring_ocr(tmp_path):
    source = make_export(tmp_path / "export", contaminated=True)
    app = tmp_path / "target"
    data, db = make_target(app)
    source_chunk_path = source / "datasets/source-kb-1/chunks/image-1.json"
    before = source_chunk_path.read_bytes()
    result = migration.import_export(source, app)
    assert result["raw_source_chunks"] == 5
    assert result["effective_source_chunks"] == result["chunks_imported"] == 2
    assert result["cleaned_count"] == result["model_error_segments_removed"] == 3
    assert result["empty_filtered"] == 3 and result["documents_without_text"] == 2
    row = imported_document(db)
    chunks = db.rows("SELECT content,metadata FROM chunks WHERE document_id=? ORDER BY ordinal", (row["id"],))
    assert chunks[0]["content"] == "合同第一句。紧随的真实OCR正文：续航10小时。"
    assert chunks[1]["content"] == "Error handling is a required device function."
    assert all("Ollama" not in chunk["content"] for chunk in chunks)
    cache = json.loads((data / "imported-text" / (row["id"] + ".json")).read_text(encoding="utf-8"))
    assert cache[1]["page_content"] == "" and cache[1]["metadata"]["raw_source_content"] == ERROR_TEXT
    assert len(cache) == 4 and source_chunk_path.read_bytes() == before
    for source_id in ("image-2", "image-3"):
        missing = imported_document(db, source_id)
        assert missing["run"] == "UNSTART" and missing["chunk_count"] == 0 and "OCR" in missing["error"]


def test_local_ocr_recovery_is_verified_in_dry_run_and_fills_only_missing_source_text(tmp_path):
    source = make_export(tmp_path / "export", contaminated=True)
    recovery = make_ocr_recovery(source, include_existing_text=True)
    app = tmp_path / "target"
    data, db = make_target(app)
    preview = migration.import_export(source, app, ocr_recovery=recovery.parent, dry_run=True)
    assert preview["dry_run"] and preview["ocr_records"] == 3
    assert preview["ocr_recovered_documents"] == preview["ocr_recovered_entries"] == 1
    assert preview["ocr_no_text_documents"] == preview["documents_without_text"] == 1
    assert preview["effective_source_chunks"] == 2 and preview["index_chunks_total"] == 3
    assert not (data / ".knowledge-import.lock").exists()
    result = migration.import_export(source, app, ocr_recovery=recovery)
    assert result["chunks_imported"] == 3 and result["documents_imported"] == 3
    assert result["ocr_recovered_characters"] > 0
    row = imported_document(db, "image-2")
    assert row["run"] == "DONE" and row["chunk_count"] == 1
    metadata = json.loads(row["metadata"])
    assert "OCR" in metadata["text_method"] and "待人工核对" in metadata["text_method"]
    assert metadata["ocr_recovery"]["applied"] and metadata["ocr_recovery"]["requires_human_verification"]
    cache = json.loads((data / "imported-text" / (row["id"] + ".json")).read_text(encoding="utf-8"))
    assert cache[0]["metadata"]["import_source"]["url"] == "http://source.example.test/"
    assert cache[0]["metadata"]["ocr_lines"][0]["words"][0]["bounding_box"] == [10, 20, 100, 25]
    assert "positions" not in cache[0]["metadata"]  # Image pixel boxes are not fabricated PDF positions.
    original_good = imported_document(db, "image-1")
    assert original_good["chunk_count"] == 2
    assert "本机 OCR" not in json.loads(original_good["metadata"])["text_method"]
    store = SQLiteVectorStore(db, LocalFeatureEmbeddings(), migration.EMBEDDING_ID, [row["dataset_id"]])
    assert any("零下20摄氏度" in doc.page_content for doc in store.similarity_search("设备温度", k=10))
    assert "OCR" in db.json_get("datasets", row["dataset_id"])["description"]
    repeated = migration.import_export(source, app, ocr_recovery=recovery)
    assert repeated["documents_imported"] == 0 and repeated["documents_reused"] == 3


@pytest.mark.parametrize("damage", ["hash", "source-hash", "outside-path", "count", "human-verification", "duplicate"])
def test_ocr_artifact_validation_failure_prevents_all_target_writes(tmp_path, damage):
    source = make_export(tmp_path / "export", contaminated=True)
    recovery = make_ocr_recovery(source)
    app = tmp_path / "target"
    data, db = make_target(app)
    value = json.loads(recovery.read_text(encoding="utf-8"))
    record = value["documents"][0]
    if damage == "hash":
        record["entries_sha256"] = "0" * 64
    elif damage == "source-hash":
        record["source_original_sha256"] = "f" * 64
    elif damage == "outside-path":
        record["entries_path"] = "../ocr.json"
    elif damage == "count":
        record["content_characters"] += 1
    elif damage == "duplicate":
        value["documents"].append(dict(record))
    else:
        entries_path = source / record["entries_path"]
        entries = json.loads(entries_path.read_text(encoding="utf-8"))
        entries[0]["metadata"]["requires_human_verification"] = False
        save_json(entries_path, entries)
        record["entries_sha256"] = migration.file_sha256(entries_path)
    save_json(recovery, value)
    with pytest.raises(migration.ImportValidationError):
        migration.import_export(source, app, ocr_recovery=recovery)
    assert not (data / ".knowledge-import.lock").exists()
    assert not db.one("SELECT name FROM sqlite_master WHERE name='knowledge_imports'")
    assert db.one("SELECT COUNT(*) AS n FROM documents")["n"] == 1


def test_repeated_import_and_changed_source_preserve_user_edits_and_deletions(tmp_path):
    source = make_export(tmp_path / "export")
    app = tmp_path / "target"
    data, db = make_target(app)
    migration.import_export(source, app)
    row = imported_document(db)
    db.execute("UPDATE documents SET name='人工修改名称.png',enabled=0 WHERE id=?", (row["id"],))
    db.execute("UPDATE chunks SET content='人工修订后的内容' WHERE document_id=?", (row["id"],))
    (data / "uploads" / row["path"]).write_bytes(b"locally-replaced-image")
    before = {table: db.rows("SELECT * FROM " + table) for table in ("documents", "chunks", "datasets", "chats", "knowledge_imports")}
    repeated = migration.import_export(source, app)
    assert repeated["documents_imported"] == 0 and repeated["documents_reused"] == 3
    assert repeated["locally_modified"] == 1 and repeated["datasets_reused"] == 2
    for table, values in before.items():
        assert db.rows("SELECT * FROM " + table) == values
    manifest_path = source / "export-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    item = manifest["datasets"][0]["documents"][0]
    original_path = source / item["original_path"]
    original_path.write_bytes(b"changed-at-source")
    item["original_bytes"] = original_path.stat().st_size
    item["original_sha256"] = migration.file_sha256(original_path)
    save_json(manifest_path, manifest)
    changed = migration.import_export(source, app)
    assert changed["documents_imported"] == 0
    assert changed["conflicts"] == [{"reason": "source_changed", "source_dataset_id": "source-kb-1", "source_document_id": "image-1"}]
    assert (data / "uploads" / row["path"]).read_bytes() == b"locally-replaced-image"
    deleted = imported_document(db, "image-2")
    db.execute("DELETE FROM documents WHERE id=?", (deleted["id"],))
    after_delete = migration.import_export(source, app)
    assert any(item["reason"] == "local_document_deleted" for item in after_delete["conflicts"])
    assert not db.one("SELECT id FROM documents WHERE id=?", (deleted["id"],))
    assert db.one("SELECT COUNT(*) AS n FROM datasets")["n"] == 3


def test_interrupted_copy_cleans_only_own_files_and_resume_does_not_duplicate(tmp_path, monkeypatch):
    source = make_export(tmp_path / "export")
    app = tmp_path / "target"
    data, db = make_target(app)
    copy = migration.KnowledgeImporter._copy_original
    interrupted = False

    def stop_after_first(self, document, destination):
        nonlocal interrupted
        if document.source_id == "disabled-1" and not interrupted:
            interrupted = True
            destination.write_bytes(b"incomplete")
            self._created_files.add(destination)
            raise KeyboardInterrupt
        return copy(self, document, destination)

    monkeypatch.setattr(migration.KnowledgeImporter, "_copy_original", stop_after_first)
    with pytest.raises(KeyboardInterrupt):
        migration.import_export(source, app)
    assert db.one("SELECT COUNT(*) AS n FROM knowledge_imports WHERE source_document_id!=''")["n"] == 1
    assert not db.rows("SELECT * FROM knowledge_import_pending")
    assert len(list((data / "uploads").iterdir())) == 2  # pre-existing + committed first file
    assert len(list((data / "imported-text").iterdir())) == 1
    resumed = migration.import_export(source, app)
    assert resumed["documents_imported"] == 2 and resumed["documents_reused"] == 1
    assert resumed["datasets_reused"] == 1 and resumed["datasets_created"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM documents")["n"] == 4
    assert (data / "uploads" / "existing.txt").is_file()


def test_failed_document_transaction_leaves_no_index_mapping_or_orphan(tmp_path, monkeypatch):
    source = make_export(tmp_path / "export", contaminated=True)
    app = tmp_path / "target"
    data, db = make_target(app)

    def invalid_vectors(*args, **kwargs):
        return [[float("nan")]]

    monkeypatch.setattr(LocalFeatureEmbeddings, "embed_documents", invalid_vectors)
    result = migration.import_export(source, app)
    assert result["documents_imported"] == 2 and len(result["failures"]) == 1
    assert not db.one("SELECT * FROM knowledge_imports WHERE source_document_id='image-1'")
    assert db.one("SELECT COUNT(*) AS n FROM chunks")["n"] == 0
    assert len(list((data / "uploads").iterdir())) == 3
    assert not db.rows("SELECT * FROM knowledge_import_pending")


def test_database_failure_after_document_and_chunks_rolls_back_both_and_own_files(tmp_path):
    source = make_export(tmp_path / "export")
    app = tmp_path / "target"
    data, db = make_target(app)
    origin, datasets = migration.prepare_export(source)
    migration.KnowledgeImporter(migration.ExistingDatabase(db.path), data, origin, datasets)._schema()
    db.execute("""CREATE TRIGGER reject_mapping BEFORE INSERT ON knowledge_imports
        WHEN NEW.source_document_id='image-1'
        BEGIN SELECT RAISE(ABORT,'synthetic transaction failure'); END""")
    result = migration.import_export(source, app)
    assert result["documents_imported"] == 2 and len(result["failures"]) == 1
    assert result["failures"][0]["error_type"] == "IntegrityError"
    assert not db.one("SELECT id FROM documents WHERE name='项目/技术协议截图.png'")
    assert not db.one("SELECT * FROM knowledge_imports WHERE source_document_id='image-1'")
    assert db.one("SELECT COUNT(*) AS n FROM chunks")["n"] == 2
    assert len(list((data / "uploads").iterdir())) == 3
    assert len(list((data / "imported-text").iterdir())) == 2
    assert not db.rows("SELECT * FROM knowledge_import_pending")


@pytest.mark.parametrize("corruption", ["hash", "traversal", "absolute", "windows-drive", "duplicate", "count"])
def test_all_source_files_are_validated_before_any_target_write(tmp_path, corruption):
    source = make_export(tmp_path / "export")
    app = tmp_path / "target"
    data, db = make_target(app)
    path = source / "export-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    # Corrupt a later item so the earlier good document must not be committed.
    item = manifest["datasets"][1]["documents"][0]
    if corruption == "hash":
        item["original_sha256"] = "0" * 64
    elif corruption == "traversal":
        item["original_path"] = "../outside.png"
    elif corruption == "absolute":
        item["original_path"] = (source / item["original_path"]).as_posix()
    elif corruption == "windows-drive":
        item["original_path"] = "C:/outside.png"
    elif corruption == "duplicate":
        manifest["datasets"][1]["documents"].append(dict(item))
    else:
        item["chunk_count"] += 1
    save_json(path, manifest)
    snapshot = {table: db.rows("SELECT * FROM " + table) for table in ("documents", "datasets", "chats", "users")}
    with pytest.raises(migration.ImportValidationError):
        migration.import_export(source, app)
    assert not db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_imports'")
    assert not (data / ".knowledge-import.lock").exists()
    for table, value in snapshot.items():
        assert db.rows("SELECT * FROM " + table) == value
    assert list((data / "uploads").iterdir()) == [data / "uploads" / "existing.txt"]


def test_target_selection_dry_run_and_cli_never_load_or_print_provider_secrets(tmp_path, monkeypatch, capsys):
    source = make_export(tmp_path / "export")
    app = tmp_path / "target"
    data, db = make_target(app, custom_dir=True, administrator=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    import os
    assert migration.main(["--export-dir", str(source), "--app-dir", str(app), "--dry-run"]) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["dry_run"] and result["source_documents"] == 3
    assert "never-print-or-load-this-secret" not in output.out + output.err
    assert "LLM_API_KEY" not in os.environ
    assert not (data / ".knowledge-import.lock").exists()
    assert not db.rows("SELECT * FROM users")
    assert migration.main(["--export-dir", str(source), "--app-dir", str(app)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["documents_imported"] == 3 and not db.rows("SELECT * FROM users")
    assert not (app / ".local-data").exists()
    assert migration.resolve_target(app, "user-data")[0] == data


def test_uninitialized_target_is_rejected_without_creating_an_account_or_database(tmp_path):
    source = make_export(tmp_path / "export")
    app = tmp_path / "empty-target"
    app.mkdir()
    with pytest.raises(migration.ImportValidationError, match="尚未初始化"):
        migration.import_export(source, app)
    assert list(app.iterdir()) == []


def test_pending_crash_recovery_only_removes_reserved_uncommitted_files(tmp_path):
    source = make_export(tmp_path / "export")
    app = tmp_path / "target"
    data, db = make_target(app)
    origin, datasets = migration.prepare_export(source)
    importer = migration.KnowledgeImporter(migration.ExistingDatabase(db.path), data, origin, datasets)
    importer._schema()
    identifier = "a" * 32
    (data / "imported-text").mkdir()
    upload_relative = "uploads/" + identifier + ".png"
    cache_relative = "imported-text/" + identifier + ".json"
    (data / upload_relative).write_bytes(b"partial-from-interrupted-import")
    (data / cache_relative).write_bytes(b"partial-cache")
    db.execute("INSERT INTO knowledge_import_pending VALUES (?,?,?,?,?,?)", (identifier, upload_relative,
        cache_relative, "0" * 64, "0" * 64, now()))
    result = migration.import_export(source, app)
    assert result["recovered_incomplete_files"] == 2
    assert not (data / upload_relative).exists() and not (data / cache_relative).exists()
    assert (data / "uploads" / "existing.txt").exists()
    assert not db.rows("SELECT * FROM knowledge_import_pending")


def test_target_upload_symlink_outside_data_directory_is_rejected(tmp_path):
    source = make_export(tmp_path / "export")
    app = tmp_path / "target"
    data, db = make_target(app)
    outside = tmp_path / "outside"
    outside.mkdir()
    # Keep this test portable on Windows accounts without symlink permission.
    try:
        (data / "imported-text").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("This account cannot create test symlinks")
    with pytest.raises(migration.ImportValidationError, match="超出"):
        migration.import_export(source, app)
    assert not list(outside.iterdir())
    assert not db.one("SELECT name FROM sqlite_master WHERE name='knowledge_imports'")
