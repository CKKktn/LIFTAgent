"""Business-rule regression tests, using isolated files and local model runnables."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from backend.config import Settings
from backend import knowledge as knowledge_module
from backend.db import Database, uid
from backend.knowledge import KnowledgeService
from backend.models import ModelRegistry
from backend.retrieval import RetrievalService
from backend.review_engine import ReviewEngine, ReviewFatalError, ReviewMaterialsChanged, ReviewOutputError, question_for
from fake_model_provider import answer_for


@pytest.fixture
def engine_workspace(tmp_path, monkeypatch):
    for name in ("ADMIN_PASSWORD", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    settings = Settings(data_dir=tmp_path)
    secret = settings.initialize()
    db = Database(tmp_path / "workspace.sqlite3")
    models = ModelRegistry(db, secret)
    models.save({"name": "synthetic-model", "base_url": "https://synthetic.invalid/v1", "api_key": "synthetic-private-key", "model_type": "chat", "set_default": True})
    knowledge = KnowledgeService(db, models, settings)
    retrieval = RetrievalService(db, models, knowledge)
    dataset = knowledge.save_dataset({"name": "合成知识库", "embedding_model": "local-features"})
    calls = []
    def respond(prompt):
        messages = [{"role": "user", "content": item.content} for item in prompt.to_messages()]
        calls.append(messages)
        return AIMessage(content=answer_for(messages))
    monkeypatch.setattr(models, "chat", lambda *args, **kwargs: RunnableLambda(respond))
    engine = ReviewEngine(db, models, knowledge, retrieval, settings)
    def add_document(name, content, parse=False, kb=None):
        path = tmp_path / "uploads" / (uid() + ".txt")
        path.write_text(content, encoding="utf-8", newline="\n")
        row = knowledge.add_document(kb or dataset["id"], name, path, path.stat().st_size)
        if parse:
            db.execute("UPDATE documents SET run='RUNNING' WHERE id=?", (row["id"],))
            knowledge._parse_one(row["id"], knowledge.document(row["id"])["generation"])
            assert knowledge.document(row["id"])["run"] == "DONE"
        return row
    baseline = add_document("技术协议.txt", "设备续航时间不得低于8小时。\n应提交设备测试报告。", True)
    plan = add_document("项目实施方案.txt", "设备采用双电池设计，设备续航时间为10小时。", True)
    report_file = add_document("设备测试报告.txt", "合成测试资料，不能代表真实项目测试。", True)
    payload = {"kb": dataset["id"], "baseline_document_ids": [baseline["id"]], "scope": "dataset", "title": "合成技术审查"}
    value = SimpleNamespace(engine=engine, db=db, models=models, knowledge=knowledge, retrieval=retrieval, dataset=dataset,
        baseline=baseline, plan=plan, report_file=report_file, payload=payload, calls=calls, add=add_document)
    yield value
    knowledge.executor.shutdown(wait=True, cancel_futures=True)


def force_answer(workspace, monkeypatch, answer):
    def respond(prompt):
        workspace.calls.append(prompt.to_messages())
        return AIMessage(content=answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False))
    monkeypatch.setattr(workspace.models, "chat", lambda *args, **kwargs: RunnableLambda(respond))


def prepared(workspace):
    return workspace.engine.prepare(workspace.payload)


@pytest.mark.parametrize("model_name", ["qwen3.7-text-embedding", "qwen3.7-text-embedding-flash"])
def test_qwen_embeds_full_parser_batch_with_provider_request_limit(engine_workspace, monkeypatch, model_name):
    saved = engine_workspace.models.save({
        "name": model_name,
        "base_url": "https://synthetic.invalid/v1",
        "model_type": "embedding",
    })
    _, embedding = engine_workspace.models.embedding(saved["id"])

    def create(input, **kwargs):
        assert len(input) <= 20, "The provider rejects a request with more than 20 inputs"
        return {
            "object": "list",
            "model": kwargs["model"],
            "data": [
                {"object": "embedding", "index": index, "embedding": [float(text.rsplit(" ", 1)[-1]), 1.0]}
                for index, text in enumerate(input)
            ],
            "usage": {"prompt_tokens": len(input), "total_tokens": len(input)},
        }

    monkeypatch.setattr(embedding.client, "create", create)
    texts = [f"合成文本 {index}" for index in range(32)]
    assert embedding.embed_documents(texts) == [[float(index), 1.0] for index in range(32)]


def test_prepare_rejects_a_material_version_changed_while_paragraphs_are_read(engine_workspace,monkeypatch):
    w=engine_workspace
    original=w.knowledge.prepare_review_source
    def changing(row):
        source=original(row)
        w.db.execute('UPDATE documents SET generation=generation+1 WHERE id=?',(row['id'],))
        return source
    monkeypatch.setattr(w.knowledge,'prepare_review_source',changing)
    with pytest.raises(ReviewMaterialsChanged):
        prepared(w)
    assert not w.calls


def extracted(workspace):
    value = prepared(workspace)
    return value, workspace.engine.extract_batch(value["report"], value["batches"][0])


def test_prepared_state_freezes_canonical_sources_without_keys_or_complete_materials(engine_workspace):
    w = engine_workspace
    value = prepared(w)
    report = value["report"]
    assert report["model"] == w.models.default_id("chat")
    assert report["baselineDocs"] == [{"id": w.baseline["id"], "name": "技术协议.txt"}]
    assert value["batches"][0]["row_start"] == 0
    serialized = json.dumps(value)
    assert "synthetic-private-key" not in serialized and '"api_key"' not in serialized
    assert "设备续航时间不得低于8小时" not in serialized
    assert set(value["batches"][0]) == {"key", "document_id", "row_start", "row_end", "depth", "source_version"}
    w.engine.assert_materials(report, value["material_signature"], value["model_signature"])


def test_preparation_rejects_empty_and_cross_dataset_scopes(engine_workspace):
    w = engine_workspace
    with pytest.raises(ReviewFatalError):
        w.engine.prepare({**w.payload, "baseline_document_ids": []})
    with pytest.raises(ReviewFatalError):
        w.engine.prepare({**w.payload, "scope": "files", "target_document_ids": [w.baseline["id"]]})
    with pytest.raises(ReviewFatalError):
        w.engine.prepare({**w.payload, "scope": "arbitrary"})


def test_extraction_uses_langchain_model_once_and_program_fills_real_paragraphs(engine_workspace):
    w = engine_workspace
    _, value = extracted(w)
    assert len(w.calls) == 1
    assert [row["type"] for row in value["requirements"]] == ["numeric", "document"]
    assert value["requirements"][0]["quote"] == "设备续航时间不得低于8小时。"
    assert value["requirements"][0]["source"]["document_id"] == w.baseline["id"]
    assert "8小时。。" not in value["requirements"][0]["question"]


def test_review_reuses_indexed_source_paragraphs_across_batches_and_service_restart(engine_workspace, monkeypatch):
    w = engine_workspace
    parsed_paths = []
    original_extract = knowledge_module.extract

    def track_extract(path, *args):
        parsed_paths.append(path)
        return original_extract(path, *args)

    monkeypatch.setattr(knowledge_module, "extract", track_extract)
    first_line = "设备续航不得低于8小时。".ljust(600, "甲")
    last_line = "故障恢复不得超过3秒。"
    baseline = w.add("长合同.txt", "\n".join([first_line, *(["乙" * 600] * 9), last_line]), parse=True)
    state = w.engine.prepare({**w.payload, "baseline_document_ids": [baseline["id"]]})
    assert [(b["row_start"], b["row_end"]) for b in state["batches"]] == [(0, 10), (10, 11)]

    restarted = KnowledgeService(w.db, w.models, w.knowledge.settings)
    engine = ReviewEngine(w.db, w.models, restarted, w.retrieval, restarted.settings)
    try:
        for batch, source_id, quote in zip(state["batches"], ("P1", "P11"), (first_line, last_line)):
            force_answer(w, monkeypatch, {"technical_indicators": [{"type": "numeric", "subject": "设备",
                "title": "技术参数", "criterion": "以合同原文为准", "rationale": "明确技术要求", "source_ids": [source_id]}],
                "closure_conditions": []})
            item, = engine.extract_batch(state["report"], batch)["requirements"]
            assert item["source_ids"] == [source_id]
            assert item["quote"] == quote
            assert item["source"]["document_id"] == baseline["id"]
    finally:
        restarted.executor.shutdown(wait=True, cancel_futures=True)
    assert len(parsed_paths) == 1


def test_unindexed_source_is_parsed_once_even_for_legacy_batches(engine_workspace, monkeypatch):
    w = engine_workspace
    parsed_paths = []
    original_extract = knowledge_module.extract

    def track_extract(path, *args):
        parsed_paths.append(path)
        return original_extract(path, *args)

    monkeypatch.setattr(knowledge_module, "extract", track_extract)
    baseline = w.add("旧版未索引合同.txt", "设备续航时间不得低于8小时。\n应提交设备测试报告。")
    state = w.engine.prepare({**w.payload, "baseline_document_ids": [baseline["id"]]})
    batch = {key: state["batches"][0][key] for key in ("key", "document_id", "row_start", "row_end", "depth")}
    for _ in range(2):
        result = w.engine.extract_batch(state["report"], batch)
        assert result["requirements"][0]["quote"] == "设备续航时间不得低于8小时。"
    assert len(parsed_paths) == 1


def test_source_cache_rejects_an_old_batch_after_the_contract_changes(engine_workspace):
    w = engine_workspace
    state = prepared(w)
    source = w.knowledge.file_path(w.knowledge.document(w.baseline["id"]))
    revised = "设备续航时间不得低于12小时。"
    source.write_text(revised + "\n应提交设备测试报告。", encoding="utf-8", newline="\n")

    with pytest.raises(ReviewMaterialsChanged):
        w.engine.extract_batch(state["report"], state["batches"][0])
    assert not w.calls

    _, extracted_text = extracted(w)
    assert extracted_text["requirements"][0]["quote"] == revised


def test_extraction_retains_valid_rows_and_isolates_bad_ids_and_non_technical_items(engine_workspace, monkeypatch):
    w = engine_workspace
    first = {"type": "numeric", "subject": "续航", "title": "续航", "criterion": "至少8小时", "rationale": "具体技术指标", "source_ids": ["P1"], "quote": "伪造内容必须忽略"}
    force_answer(w, monkeypatch, {"technical_indicators": [first, {**first, "title": "伪造", "source_ids": ["P999"]}, 3,
        {**first, "subject": "合同总价", "title": "合同总价"}], "closure_conditions": []})
    _, value = extracted(w)
    assert len(value["requirements"]) == 1 and value["rejected"] == 2
    assert value["requirements"][0]["quote"] != first["quote"]
    assert value["filtered"][0]["title"] == "合同总价"
    assert value["issues"][0]["retained"] == 1


def test_malformed_json_is_a_single_attempt_and_never_echoes_provider_text(engine_workspace, monkeypatch):
    w = engine_workspace
    force_answer(w, monkeypatch, "not json: secret=synthetic-private-key")
    state = prepared(w)
    with pytest.raises(ReviewOutputError) as error:
        w.engine.extract_batch(state["report"], state["batches"][0])
    assert len(w.calls) == 1 and "synthetic-private-key" not in str(error.value)


def test_batch_split_covers_all_rows_once_and_has_stable_child_keys(engine_workspace):
    batch = {"key": "doc:P1", "document_id": "doc", "row_start": 0, "row_end": 5, "depth": 0}
    values = engine_workspace.engine.split_batch(batch)
    assert [(item["row_start"], item["row_end"]) for item in values] == [(0, 3), (3, 5)]
    assert [item["key"] for item in values] == ["doc:P1/0", "doc:P1/1"]
    assert all(item["depth"] == 1 for item in values)
    assert not engine_workspace.engine.split_batch({**batch, "row_end": 1})


def test_merge_and_confirmation_do_not_duplicate_or_accept_forged_source_fields(engine_workspace):
    w = engine_workspace
    state, output = extracted(w)
    report = state["report"]
    report["requirements"] = output["requirements"]
    assert len(w.engine.merge_requirements(output["requirements"], output["requirements"])) == 2
    edited = copy.deepcopy(output["requirements"])
    edited[0].update(criterion="续航至少9小时。", quote="伪造原文", source={"document_id": "evil"}, question="伪造问句")
    confirmed = w.engine.confirm_requirements(report, edited)
    assert confirmed[0]["quote"] == output["requirements"][0]["quote"]
    assert confirmed[0]["source"] == output["requirements"][0]["source"]
    assert confirmed[0]["userEdited"] and "至少9小时" in confirmed[0]["question"]
    assert edited[0]["quote"] == "伪造原文"


def test_confirmation_accepts_explicit_manual_conditions_and_rejects_unknown_rows(engine_workspace):
    w = engine_workspace
    report = prepared(w)["report"]
    item = {"id": "R900", "type": "functional", "category": "功能条件", "title": "离线缓存", "criterion": "支持离线缓存", "userAdded": True, "source": {"api_key": "injected"}}
    result = w.engine.confirm_requirements(report, [item])
    assert result[0]["id"] == "R001" and result[0]["userAdded"]
    assert result[0]["source"] == {"document_name": "人工补充"}
    with pytest.raises(ReviewOutputError):
        w.engine.confirm_requirements(report, [{**item, "userAdded": False}])
    with pytest.raises(ReviewOutputError):
        w.engine.confirm_requirements(report, [{**item, "category": "数值指标"}])


def test_confirmation_keeps_origin_after_first_requirement_is_unchecked(engine_workspace):
    w = engine_workspace
    state, output = extracted(w)
    report = {**state["report"], "requirements": output["requirements"]}
    original = output["requirements"][1]
    candidate = {**copy.deepcopy(original), "id": "R001", "originId": "R002", "quote": "伪造原文", "source": {"document_id": "wrong"}, "rationale": "伪造理由"}
    result = w.engine.confirm_requirements(report, [candidate])
    assert result[0]["id"] == "R001" and result[0]["originId"] == "R002"
    for key in ("quote", "source", "rationale"):
        assert result[0][key] == original[key]
    assert result[0]["type"] == "document"


def test_manual_requirement_with_renumbered_id_never_binds_to_an_extracted_source(engine_workspace):
    w = engine_workspace
    state, output = extracted(w)
    report = {**state["report"], "requirements": output["requirements"]}
    candidate = {"id": "R001", "originId": "R001", "userAdded": True, "type": "functional", "category": "功能条件", "title": "离线缓存", "criterion": "支持离线缓存", "quote": output["requirements"][0]["quote"], "source": output["requirements"][0]["source"]}
    result = w.engine.confirm_requirements(report, [candidate])
    assert result[0]["originId"] is None and result[0]["userAdded"]
    assert result[0]["source"] == {"document_name": "人工补充"}
    assert result[0]["quote"] == "人工补充指标，请人工核对合同依据"


def test_prevalidated_confirmation_is_idempotent_for_graph_validation(engine_workspace):
    w = engine_workspace
    state, output = extracted(w)
    report = {**state["report"], "requirements": output["requirements"]}
    kept = {**copy.deepcopy(output["requirements"][1]), "id": "R001", "originId": "R002", "criterion": "须提供测试报告的正式版本。"}
    manual = {"id": "R002", "originId": None, "userAdded": True, "type": "functional", "category": "功能条件", "title": "离线缓存", "criterion": "支持离线缓存"}
    first = w.engine.confirm_requirements(report, [kept, manual])
    second = w.engine.confirm_requirements(report, first)
    third = w.engine.confirm_requirements({**report, "requirements": first}, second)
    assert first == second == third
    whitespace_only = {**copy.deepcopy(output["requirements"][0]), "title": "  " + output["requirements"][0]["title"] + "  ", "originId": "R001"}
    normalized = w.engine.confirm_requirements(report, [whitespace_only])
    assert normalized == w.engine.confirm_requirements(report, normalized)


def test_confirmation_rejects_duplicate_origin_identity_after_renumbering(engine_workspace):
    w = engine_workspace
    state, output = extracted(w)
    report = {**state["report"], "requirements": output["requirements"]}
    first = {**copy.deepcopy(output["requirements"][0]), "id": "R001", "originId": "R001"}
    duplicate = {**copy.deepcopy(first), "id": "R002"}
    with pytest.raises(ReviewOutputError):
        w.engine.confirm_requirements(report, [first, duplicate])


def test_material_change_detects_chunk_edits_without_document_timestamp_update(engine_workspace):
    w = engine_workspace
    value = prepared(w)
    w.db.execute("UPDATE chunks SET content=content || 'changed' WHERE document_id=?", (w.plan["id"],))
    with pytest.raises(ReviewMaterialsChanged):
        w.engine.assert_materials(value["report"], value["material_signature"], value["model_signature"])


def test_model_credential_change_invalidates_state_without_exposing_credential(engine_workspace):
    w = engine_workspace
    value = prepared(w)
    model = w.models.find(value["report"]["model"])
    w.models.save({**model, "api_key": "another-synthetic-key"})
    with pytest.raises(ReviewMaterialsChanged) as error:
        w.engine.assert_materials(value["report"], value["material_signature"], value["model_signature"])
    assert "another-synthetic-key" not in str(error.value)


def test_real_langchain_retriever_excludes_baseline_and_respects_restricted_scope(engine_workspace):
    w = engine_workspace
    state, output = extracted(w)
    requirement = output["requirements"][0]
    scoped = {**state["report"], "scope": "files", "targetDocs": [{"id": w.plan["id"], "name": w.plan["name"]}]}
    chunks = w.engine.retrieve(scoped, requirement, 0, [], "")
    assert chunks and {row["document_id"] for row in chunks} == {w.plan["id"]}
    assert chunks[0]["reviewId"] == "E1" and "10小时" in chunks[0]["content"]
    assert w.baseline["id"] not in {row["document_id"] for row in chunks}


def test_complementary_retrieval_preserves_ids_and_appends_new_evidence(engine_workspace, monkeypatch):
    w = engine_workspace
    state, output = extracted(w)
    documents = [Document(page_content="方案续航10小时。", metadata={"document_id": w.plan["id"], "id": "first", "dataset_id": w.dataset["id"]})]
    monkeypatch.setattr(w.retrieval, "search", lambda options: {"chunks": [{**row.metadata, "content": row.page_content} for row in documents]})
    first = w.engine.retrieve(state["report"], output["requirements"][0], 0, [], "")
    documents.append(Document(page_content="补充测试材料。", metadata={"document_id": w.report_file["id"], "id": "second", "dataset_id": w.dataset["id"]}))
    second = w.engine.retrieve(state["report"], output["requirements"][0], 1, first, "适用条件待核实")
    assert second[:len(first)] == first and len(second) == 2
    assert [row["reviewId"] for row in second] == ["E1", "E2"]
    assert len(first) == 1
    with pytest.raises(ReviewOutputError):
        w.engine.retrieve(state["report"], output["requirements"][0], 2, second, "")


def test_quick_review_keeps_16_sources_per_round_and_late_measurements(engine_workspace, monkeypatch):
    w = engine_workspace
    state, output = extracted(w)
    requests = []

    def search(options):
        requests.append(options)
        prefix = len(requests)
        return {"chunks": [{"id": f"{prefix}-{index}", "document_id": w.plan["id"],
                            "dataset_id": w.dataset["id"],
                            "content": "设备批次A。\n" + "一般介绍。" * 1600 + "\n满载复测：设备续航时间为6小时。"}
                           for index in range(20)]}

    monkeypatch.setattr(w.retrieval, "search", search)
    requirement = output["requirements"][0]
    first = w.engine.retrieve(state["report"], requirement, 0, [], "")
    combined = w.engine.retrieve(state["report"], requirement, 1, first, "", countercheck=True)
    assert len({row["id"] for row in first}) == 16
    assert len({row["id"] for row in combined}) == 32
    assert {row["id"] for row in combined if "续航时间为6小时" in row["content"]} == {row["id"] for row in combined}
    assert combined[:len(first)] == first
    assert [request["page_size"] for request in requests] == [16, 16]
    assert "复测" in requests[1]["question"]
    assert set(requests[1]["exclude_chunk_ids"]) == {row["id"] for row in first}


def test_judgment_refills_program_original_and_downgrades_invalid_reference(engine_workspace, monkeypatch):
    w = engine_workspace
    state, output = extracted(w)
    requirement = output["requirements"][0]
    chunks = [{"reviewId": "E1", "document_id": w.plan["id"], "document_name": "项目实施方案.txt", "dataset_id": w.dataset["id"], "content": "续航10小时。"}]
    force_answer(w, monkeypatch, {"status": "已满足", "project_plan": "方案10小时", "analysis": "10不低于8", "gaps": "无", "recommendation": "复核", "evidence": [
        {"source_id": "E1", "kind": "支持", "analysis": "方案参数", "quote": "伪造改写", "source": {"api_key": "injected"}},
        {"source_id": "E99", "kind": "反证", "analysis": "无效来源"}, "invalid", {"source_id": ["E1"], "kind": "支持", "analysis": "无效数组编号"}, {"source_id": "E1", "kind": {}, "analysis": "无效分类"}]})
    result = w.engine.judge(state["report"], requirement, chunks)
    assert result["status"] == "待人工复核" and result["retryable"]
    assert len(result["evidence"]) == 1 and result["evidence"][0]["quote"] == "续航10小时。"
    assert "injected" not in json.dumps(result) and "伪造改写" not in json.dumps(result)


@pytest.mark.parametrize("status,kind", [("已满足", "计划"), ("不满足", "背景"), ("证据冲突", "支持")])
def test_unsubstantiated_verdict_is_never_accepted(engine_workspace, monkeypatch, status, kind):
    w = engine_workspace
    state, output = extracted(w)
    force_answer(w, monkeypatch, {"status": status, "analysis": "需要核对", "evidence": [{"source_id": "E1", "kind": kind, "analysis": "材料性质"}]})
    result = w.engine.judge(state["report"], output["requirements"][0], [{"reviewId": "E1", "document_id": w.plan["id"], "document_name": "方案.txt", "content": "方案参数"}])
    assert result["status"] == "待人工复核"


def test_no_evidence_is_insufficient_without_model_call_or_false_failure(engine_workspace):
    w = engine_workspace
    state, output = extracted(w)
    count = len(w.calls)
    result = w.engine.judge(state["report"], output["requirements"][0], [])
    assert result["status"] == "证据不足" and len(w.calls) == count


@pytest.mark.parametrize(("basis", "expected"), [
    ("可比实测", "不满足"),
    ("", "已满足"),
])
def test_applicable_measured_failure_takes_precedence_over_design(engine_workspace, monkeypatch, basis, expected):
    w = engine_workspace
    state, output = extracted(w)
    chunks = [
        {"reviewId": "E1", "document_name": "方案.txt", "content": "X7 设计续航 10 小时。"},
        {"reviewId": "E2", "document_name": "测试.txt", "content": "X7 额定负载下实测 7.6 小时。"},
    ]
    force_answer(w, monkeypatch, {"status": "已满足", "analysis": "设计续航 10 小时达到要求（E1）。", "evidence": [
        {"source_id": "E1", "kind": "支持", "analysis": "设计值达到 8 小时门槛。"},
        {"source_id": "E2", "kind": "反证" if basis else "背景", "basis": basis,
         "analysis": "实测 7.6 小时低于 8 小时。"},
    ]})
    result = w.engine.judge(state["report"], output["requirements"][0], chunks)
    assert result["status"] == expected
    assert result["evidence"][1]["quote"] == chunks[1]["content"]
    if basis:
        assert "7.6 小时低于 8 小时" in result["analysis"]
        assert "E2" in result["analysis"]
    assert "可比实测" in w.calls[-1][-1].content


def test_document_check_matches_actual_name_only_without_requiring_parsing(engine_workspace):
    w = engine_workspace
    state, output = extracted(w)
    w.db.execute("UPDATE documents SET run='UNSTART' WHERE id=?", (w.report_file["id"],))
    count = len(w.calls)
    result = w.engine.check_document(state["report"], output["requirements"][1])
    assert result["status"] == "已满足" and result["checkType"] == "document"
    assert result["evidence"][0]["quote"] == "设备测试报告.txt"
    assert len(w.calls) == count


def test_document_check_does_not_use_contract_itself_or_related_body(engine_workspace, monkeypatch):
    w = engine_workspace
    state, output = extracted(w)
    force_answer(w, monkeypatch, {"matches": []})
    report = {**state["report"], "scope": "files", "targetDocs": [{"id": w.plan["id"], "name": w.plan["name"]}]}
    result = w.engine.check_document(report, {**output["requirements"][1], "document_name": "技术协议"})
    assert result["status"] == "证据不足" and not result["evidence"]


def test_polish_never_changes_ids_status_or_exports_extra_model_fields(engine_workspace, monkeypatch):
    w = engine_workspace
    report = prepared(w)["report"]
    rows = [{"id": "R001", "question": "合成问句", "answer": {"status": "不满足", "analysis": "有不符事实", "recommendation": "修改"}}]
    force_answer(w, monkeypatch, {"sections": [{"id": "R001", "status": "已满足", "headline": "标题", "summary": "摘要", "action": "行动"}]})
    with pytest.raises(ReviewOutputError):
        w.engine.polish_batch(report, rows)
    force_answer(w, monkeypatch, {"sections": [{"id": "R001", "status": "不满足", "headline": "标题", "summary": "摘要", "action": "行动", "api_key": "injected"}]})
    result = w.engine.polish_batch(report, rows)
    assert set(result[0]) == {"id", "status", "headline", "summary", "action"}


def test_generated_questions_keep_decimal_units_and_exact_source_punctuation():
    question = question_for({"type": "numeric", "criterion": "压力至少0.5 MPa，续航8.5小时。。"})
    assert "0.5 MPa" in question and "8.5小时。当前" in question and "小时。。" not in question


@pytest.mark.parametrize("summary", ["方案为99小时", "根据来源E99判断"])
def test_polished_text_cannot_add_numbers_or_reference_ids(engine_workspace, monkeypatch, summary):
    w = engine_workspace
    report = prepared(w)["report"]
    rows = [{"id": "R001", "question": "续航8小时", "answer": {"status": "已满足", "analysis": "方案10小时，来源E1", "recommendation": "复核"}}]
    force_answer(w, monkeypatch, {"sections": [{"id": "R001", "status": "已满足", "headline": "续航条件", "summary": summary, "action": "复核"}]})
    with pytest.raises(ReviewOutputError):
        w.engine.polish_batch(report, rows)
