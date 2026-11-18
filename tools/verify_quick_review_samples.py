"""Check the synthetic pack against v2 in a temporary, offline workspace."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PACK = ROOT / "output" / "pdf" / "快速审查测试包"


def verify():
    # Changes affect this verification process only. Never load .env or live data.
    for name in list(os.environ):
        if name.startswith(("ADMIN_", "LLM_", "EMBEDDING_", "LANGSMITH_", "LANGCHAIN_")):
            os.environ.pop(name, None)
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"

    import httpx
    from fastapi.testclient import TestClient
    from backend.config import Settings
    from backend.main import create_app
    from backend.parsers import extract

    truth = json.loads((PACK / "03_核对资料_不要上传/expected-results.json").read_text(encoding="utf-8"))
    result = {"scope": "v2 synthetic upload, parsing, retrieval and baseline exclusion only",
        "external_model_calls": 0, "real_model_review_evaluated": False,
        "temporary_workspace": True, "parsed_unique_files": [], "cases": []}
    paths = sorted({relative for case in truth["cases"] for relative in case["upload_files"]})
    for relative in paths:
        text = "\n".join(item.page_content for item in extract(PACK / relative))
        assert len(text) > 60, relative
        if relative == truth["baseline"]:
            assert all(row["id"] in text for row in truth["requirements"])
            assert all(token in text for token in ["25±2", "200", "CSV", "保密承诺书", "-10", "+50"])
        result["parsed_unique_files"].append({"file": relative, "characters": len(text)})

    def blocked(*args, **kwargs):
        raise AssertionError("An external HTTP request was attempted during offline fixture validation")

    async def blocked_async(*args, **kwargs):
        blocked()

    def data(response):
        assert response.status_code == 200, response.text
        value = response.json()
        assert value.get("code") == 0, value
        return value["data"]

    with tempfile.TemporaryDirectory(prefix="quick-review-samples-") as directory:
        settings = Settings(data_dir=Path(directory), frontend_dir=ROOT / "frontend")
        with patch.object(httpx.HTTPTransport, "handle_request", blocked), patch.object(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async):
            app = create_app(settings)
            with TestClient(app) as client:
                assert client.post("/auth/setup", json={"username": "sample-tester", "password": "synthetic-only-password"}).status_code == 200
                for case in truth["cases"]:
                    dataset = data(client.post("/api/v1/datasets", json={"name": case["knowledge_base_name"], "embedding_model": "local-features", "chunk_method": "naive", "parser_config": {"chunk_size": 1000, "chunk_overlap": 120}}))["id"]
                    uploaded = []
                    baseline_ids = []
                    for relative in case["upload_files"]:
                        file = PACK / relative
                        with file.open("rb") as stream:
                            item = data(client.post(f"/api/v1/datasets/{dataset}/documents", files={"file": (file.name, stream, "application/pdf" if file.suffix == ".pdf" else "text/plain")}))[0]
                        uploaded.append(item)
                        if relative in case["baseline_files"]:
                            baseline_ids.append(item["id"])
                    data(client.post(f"/api/v1/datasets/{dataset}/chunks", json={"document_ids": [item["id"] for item in uploaded]}))
                    deadline = time.monotonic() + 40
                    while time.monotonic() < deadline:
                        catalog = data(client.get(f"/api/v1/datasets/{dataset}/documents", params={"page_size": 100}))["docs"]
                        if all(item["run"] == "DONE" for item in catalog):
                            break
                        assert not any(item["run"] == "FAIL" for item in catalog), catalog
                        time.sleep(0.03)
                    else:
                        raise AssertionError("Parsing deadline exceeded for " + case["id"])

                    checks = []
                    for requirement in truth["requirements"]:
                        if requirement["type"] == "document":
                            name = requirement["document_name"]
                            matches = [item["name"] for item in catalog if item["id"] not in baseline_ids and name in Path(item["name"]).stem]
                            expected = requirement["expected"][case["id"]]["status"]
                            assert bool(matches) == (expected == "已满足"), (case["id"], requirement["id"], matches)
                            checks.append({"id": requirement["id"], "kind": "filename_presence", "matched_files": matches})
                            continue
                        question = requirement["subject"] + " " + requirement["criterion"]
                        found = data(client.post("/api/v1/retrieval", json={"question": question, "dataset_ids": [dataset], "exclude_document_ids": baseline_ids,
                            "page_size": 32, "similarity_threshold": 0.2, "vector_similarity_weight": 0.3}))
                        chunks = found["chunks"][:8]
                        assert not any(item["document_id"] in baseline_ids for item in found["chunks"])
                        assert all(item["dataset_id"] == dataset for item in found["chunks"])
                        files = sorted({item["document_name"] for item in chunks})
                        if case["id"] == "baseline_only":
                            assert not chunks, requirement["id"]
                        else:
                            plan_names = [Path(relative).name for relative in case["upload_files"] if "项目实施方案" in relative]
                            required_plans = plan_names if case["id"] == "conflicting" and requirement["id"] == "T02" else plan_names[:1]
                            assert set(required_plans) <= set(files), (case["id"], requirement["id"], files)
                            if requirement["id"] == "T02":
                                text = "\n".join(chunk["content"] for chunk in chunks)
                                assert "-5" in text if case["id"] == "mixed" else "-10" in text
                                if case["id"] == "conflicting":
                                    assert "-5" in text and "-10" in text
                        checks.append({"id": requirement["id"], "kind": "first_round_retrieval", "top_8_files": files, "chunks": len(chunks)})
                    result["cases"].append({"id": case["id"], "documents": len(catalog), "chunks": sum(item["chunk_count"] for item in catalog), "checks": checks})
    result["passed"] = True
    target = PACK / "素材验证记录.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": True, "unique_files": len(paths), "case_documents": sum(case["documents"] for case in result["cases"]),
        "checks": sum(len(case["checks"]) for case in result["cases"]), "external_model_calls": 0}, ensure_ascii=True))


if __name__ == "__main__":
    verify()
