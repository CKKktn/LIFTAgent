from pathlib import Path
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from export_knowledge import KnowledgeExport, SourceClient, ExportError, identifier, safe_name


class FakeKnowledge:
    base = "http://example.test/"

    def __init__(self):
        self.chunk_reads = self.downloads = 0
        self.version = "v1"

    def pages(self, path, field=None):
        if path == "/api/v1/datasets":
            return [{"id": "kb1", "name": "测试库", "document_count": 2, "chunk_count": 2, "api_key": "do-not-export"}]
        if path == "/api/v1/datasets/kb1/documents":
            return [{"id": "d1", "name": "../CON.docx", "size": 8, "chunk_count": 1, "update_time": self.version},
                {"id": "d2", "name": "目录/乱码\ufffd.txt", "size": 8, "chunk_count": 1, "update_time": self.version}]
        self.chunk_reads += 1
        return [{"id": "c1", "content": "真实来源原文，不由模型生成。", "page_number": 2}]

    def original(self, dataset_id, document_id, destination):
        assert dataset_id == "kb1"
        self.downloads += 1
        Path(destination).write_bytes(b"\x00\xffexact-original-" + document_id.encode())
        return {"content-type": "application/octet-stream"}


def test_export_keeps_exact_bytes_sources_and_safe_paths(tmp_path):
    client = FakeKnowledge()
    export = KnowledgeExport(client, tmp_path)
    export.inventory()
    export.export()
    summary = export.report()
    assert summary["originals_saved"] == 2 and summary["chunks_saved"] == 2
    assert summary["source_filename_encoding_issues"] == 1
    for document in export.manifest["datasets"][0]["documents"]:
        path = tmp_path / document["original_path"]
        assert path.resolve().is_relative_to(tmp_path.resolve())
        assert path.suffix in {".docx", ".txt"}
        assert path.read_bytes() == b"\x00\xffexact-original-" + document["id"].encode()
        assert "真实来源原文" in (tmp_path / document["text_path"]).read_text(encoding="utf-8")
    config = json.loads(next(tmp_path.rglob("dataset.json")).read_text(encoding="utf-8"))
    assert config["api_key"] == "[REDACTED]"


def test_resume_verifies_hashes_and_refreshes_changed_source(tmp_path):
    client = FakeKnowledge()
    export = KnowledgeExport(client, tmp_path)
    export.inventory()
    export.export()
    assert (client.chunk_reads, client.downloads) == (2, 2)
    export.export()
    assert (client.chunk_reads, client.downloads) == (2, 2)
    row = export.manifest["datasets"][0]["documents"][0]
    (tmp_path / row["text_path"]).write_text("corrupted", encoding="utf-8")
    export.export()
    assert (client.chunk_reads, client.downloads) == (3, 2)
    client.version = "v2"
    export.inventory()
    export.export()
    assert (client.chunk_reads, client.downloads) == (5, 4)


def test_pagination_follows_reported_total_and_rejects_repeating_pages():
    client = object.__new__(SourceClient)
    first = [{"id": "d" + str(i)} for i in range(100)]
    client.api = lambda path: {"total": 101, "docs": first if "page=1&" in path else [{"id": "d100"}]}
    assert len(client.pages("/api/v1/datasets/kb1/documents", "docs")) == 101
    client.api = lambda path: {"total": 101, "docs": first}
    with pytest.raises(ExportError, match="repeated"):
        client.pages("/api/v1/datasets/kb1/documents", "docs")


def test_export_client_rejects_mutating_or_unrelated_operations(tmp_path):
    client = SourceClient("http://example.test", tmp_path)
    with pytest.raises(ValueError):
        client.request("/api/v1/datasets", payload=b"{}")
    with pytest.raises(ValueError):
        client.api("/api/v1/models")
    with pytest.raises(ValueError):
        identifier("../outside")
    assert safe_name("CON") == "_CON"
