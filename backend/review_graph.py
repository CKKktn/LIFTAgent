"""Durable quick-review state graphs. Business/model code lives in ReviewEngine.

All node outputs contain JSON values, never model credentials. Interrupt nodes
have no external side effects: LangGraph re-enters them on Command(resume=...).
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .db import now
from .review_engine import ReviewFatalError, ReviewMaterialsChanged

WORKFLOW_VERSION = "quick-review-v1"
MAX_MODEL_ATTEMPTS = 2
MAX_RETRIEVAL_ROUNDS = 2

NODE_LABELS = {
    "__start__": "开始", "__end__": "结束", "initialize": "固定任务输入与资料版本",
    "extract_requirements": "提取与确认子图", "select_batch": "选择尚未完成的批次",
    "extract_batch": "LangChain 提取与原文校验", "merge_batch": "保留有效项、过滤与去重",
    "split_or_retry": "拆小批次或有限重试", "record_batch_issue": "隔离异常并保留有效批次",
    "extraction_finish": "保存提取清单", "await_confirmation": "暂停等待人工确认",
    "validate_confirmation": "校验人工修订与材料版本", "check_requirements": "单条要求核查子图",
    "select_requirement": "分派下一条已确认要求", "document_check": "核对技术文档文件名",
    "numeric_retrieve": "检索数值方案证据", "functional_retrieve": "检索功能实现证据",
    "judge_numeric": "比较参数、单位及条件", "judge_functional": "核对功能实现机制",
    "assess_evidence": "判断是否需要补查反证或证据缺口", "supplement_retrieval": "补查反证或证据缺口（两轮内）",
    "item_failure": "保留异常供人工复核", "save_item": "按条目编号保存结果",
    "checking_done": "逐项核查结束", "report_workflow": "报告整理子图",
    "collect_report": "核对版本并固定原始报告", "polish_one": "整理并校验展示内容",
    "polish_fallback": "整理失败时保留原始分析", "advance_report": "处理下一报告批次",
    "finalize_report": "保存报告与完整边界说明", "await_retry": "完成后可选重试异常项",
    "restart_retry": "只重试异常、复用成功结果",
}


class ReviewStopped(Exception):
    """A user-requested stop at a checkpoint-safe node boundary."""


class ReviewStale(Exception):
    """The frozen material or model version no longer matches."""


class ReviewState(TypedDict, total=False):
    task_id: str
    request: dict
    workflow_version: str
    report: dict
    material_signature: str
    model_signature: str
    phase: str
    stage: int
    message: str
    extraction_queue: list
    extraction_done: list
    batch: dict
    batch_value: dict
    batch_ok: bool
    batch_attempts: dict
    batch_error: str
    confirmation: Any
    approved_requirements: str
    checked_ids: list
    current_requirement: dict
    current_chunks: list
    current_result: dict
    round_index: int
    countercheck: bool
    judge_attempts: int
    judge_ok: bool
    retrieval_changed: bool
    item_error: str
    report_groups: list
    report_cursor: int
    polish_cache: dict
    polish_failed: list
    polish_attempts: int
    polish_ok: bool
    retry_available: bool
    retry_requested: bool
    no_requirements: bool


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _report(state):
    return deepcopy(state.get("report", {}))


def _issue(report, phase, reason, **metadata):
    issue = {"phase": phase, "reason": reason, **metadata}
    issue["key"] = _digest(issue)
    if not any(item.get("key") == issue["key"] for item in report.setdefault("issues", [])):
        report["issues"].append(issue)


def _clear_item_issue(report, identifier):
    report["issues"] = [item for item in report.get("issues", []) if not (item.get("phase") == "指标检索判定" and item.get("requirementId") == identifier)]


def build_review_graph(engine, checkpointer, controls=None):
    def assert_versions(state):
        try:
            engine.assert_materials(state["report"], state["material_signature"], state["model_signature"])
        except ReviewMaterialsChanged as error:
            raise ReviewStale("资料或模型配置已变更，不能混用原任务结论；请新建任务。") from error

    def guarded(name, function):
        def node(state):
            if controls and controls.should_stop(state["task_id"]):
                raise ReviewStopped("已在步骤边界停止，完成的结果和检查点已保留。")
            if state.get("workflow_version") and state["workflow_version"] != WORKFLOW_VERSION:
                raise ReviewStale("任务工作流版本不兼容，请新建任务。")
            if state.get("report") and state.get("material_signature") and state.get("model_signature"):
                assert_versions(state)
            try:
                return function(state)
            except ReviewMaterialsChanged as error:
                raise ReviewStale("资料或模型配置已变更，不能混用原任务结论；请新建任务。") from error
        node.__name__ = name
        return node

    def initialize(state):
        prepared = engine.prepare(state["request"])
        report = deepcopy(prepared["report"])
        report.update(workflowJobId=state["task_id"], workflowVersion=WORKFLOW_VERSION, complete=False,
                      awaitingConfirmation=False, confirmed=False)
        return {"report": report, "workflow_version": WORKFLOW_VERSION,
                "material_signature": prepared["material_signature"], "model_signature": prepared["model_signature"],
                "extraction_queue": prepared["batches"], "extraction_done": [], "batch_attempts": {},
                "checked_ids": [], "polish_cache": {}, "polish_failed": [], "no_requirements": False,
                "phase": "extracting", "stage": 1, "message": "已固定资料与模型版本，开始提取技术要求。"}

    def select_batch(state):
        assert_versions(state)
        queue = state.get("extraction_queue", [])
        return {"batch": queue[0] if queue else {}, "phase": "extracting", "stage": 1,
                "message": f"提取指标与结题条件 · 已保留 {len(state['report'].get('requirements', []))} 项"}

    def extract_batch(state):
        attempts = dict(state.get("batch_attempts", {}))
        key = state["batch"]["key"]
        attempts[key] = attempts.get(key, 0) + 1
        try:
            value = engine.extract_batch(state["report"], state["batch"])
            return {"batch_value": value, "batch_ok": True, "batch_error": "", "batch_attempts": attempts}
        except ReviewFatalError:
            raise
        except Exception:
            return {"batch_value": {}, "batch_ok": False, "batch_error": "本批结构或来源检查未通过，已限制重试。", "batch_attempts": attempts}

    def merge_batch(state):
        report = _report(state)
        value = state["batch_value"]
        report["requirements"] = engine.merge_requirements(report.get("requirements", []), value.get("requirements", []))
        report.setdefault("filteredRequirements", []).extend(value.get("filtered", []))
        for issue in value.get("issues", []):
            if issue not in report.setdefault("issues", []):
                report["issues"].append(issue)
        if value.get("rejected") and not value.get("issues"):
            _issue(report, "提取要求", f"隔离 {value['rejected']} 条缺少有效条件或原文来源的记录。",
                   sources=[{"id": state["batch"]["key"], "file": state["batch"].get("document_id", "")}])
        done = list(dict.fromkeys([*state.get("extraction_done", []), state["batch"]["key"]]))
        report["extractedParts"] = done
        return {"report": report, "extraction_done": done, "extraction_queue": state["extraction_queue"][1:], "batch_value": {}}

    def split_or_retry(state):
        report = _report(state)
        batch = state["batch"]
        children = engine.split_batch(batch) if not batch.get("depth", 0) else []
        if len(children) > 1:
            report.setdefault("splitParts", []).append(batch["key"])
            return {"report": report, "extraction_queue": children + state["extraction_queue"][1:],
                    "message": "本批输出异常，拆成较小批次；已成功部分不重跑。"}
        return {"message": "本批输出检查未通过，进行一次有限重试。"}

    def failure_route(state):
        if state["extraction_queue"] and state["extraction_queue"][0]["key"] != state["batch"]["key"]:
            return "select_batch"
        return "extract_batch" if state["batch_attempts"].get(state["batch"]["key"], 0) < MAX_MODEL_ATTEMPTS else "record_batch_issue"

    def record_batch_issue(state):
        report = _report(state)
        _issue(report, "提取要求", "本批经有限拆分或重试仍未通过；有效批次已保留，请人工补充或核对材料。",
               sources=[{"id": state["batch"]["key"], "file": state["batch"].get("document_id", "")}], retained=0)
        return {"report": report, "extraction_queue": state["extraction_queue"][1:]}

    def extraction_finish(state):
        report = _report(state)
        no_requirements = not report.get("requirements")
        report.update(extractionComplete=not any(item.get("phase") == "提取要求" for item in report.get("issues", [])),
                      awaitingConfirmation=not no_requirements, complete=False)
        return {"report": report, "no_requirements": no_requirements, "phase": "failed" if no_requirements else "waiting_confirmation",
                "stage": 1, "message": "未取得可验证技术要求，请检查解析内容后新建任务。" if no_requirements else "技术要求已提取，等待人工修改与确认。"}

    extract = StateGraph(ReviewState)
    for name, function in {"select_batch": select_batch, "extract_batch": extract_batch, "merge_batch": merge_batch,
                           "split_or_retry": split_or_retry, "record_batch_issue": record_batch_issue, "extraction_finish": extraction_finish}.items():
        extract.add_node(name, guarded(name, function))
    extract.add_edge(START, "select_batch")
    extract.add_conditional_edges("select_batch", lambda state: "extract_batch" if state["batch"] else "extraction_finish", ["extract_batch", "extraction_finish"])
    extract.add_conditional_edges("extract_batch", lambda state: "merge_batch" if state["batch_ok"] else "split_or_retry", ["merge_batch", "split_or_retry"])
    extract.add_edge("merge_batch", "select_batch")
    extract.add_conditional_edges("split_or_retry", failure_route, ["select_batch", "extract_batch", "record_batch_issue"])
    extract.add_edge("record_batch_issue", "select_batch")
    extract.add_edge("extraction_finish", END)

    def await_confirmation(state):
        response = interrupt({"type": "confirm_requirements", "task_id": state["task_id"],
                              "requirements": state["report"].get("requirements", []),
                              "message": "请核对并确认技术要求，确认前不会核查项目材料。"})
        return {"confirmation": response}

    def validate_confirmation(state):
        assert_versions(state)
        response = state.get("confirmation")
        if not isinstance(response, dict) or not isinstance(response.get("requirements"), list):
            raise ValueError("人工确认缺少有效要求清单。")
        report = _report(state)
        report["requirements"] = engine.confirm_requirements(report, response["requirements"])
        report.update(confirmed=True, awaitingConfirmation=False, results=[], polished=[], complete=False)
        approved = _digest(report["requirements"])
        report["approvedRequirementsVersion"] = approved
        return {"report": report, "confirmation": None, "approved_requirements": approved, "checked_ids": [],
                "phase": "checking", "stage": 2, "message": "人工修订已验证，开始按要求类型逐条核查。"}

    def select_requirement(state):
        assert_versions(state)
        if _digest(state["report"]["requirements"]) != state.get("approved_requirements"):
            raise ReviewStale("已确认要求发生变化，不能复用旧结果。")
        checked = set(state.get("checked_ids", []))
        requirement = next((item for item in state["report"]["requirements"] if item["id"] not in checked), {})
        return {"current_requirement": requirement, "current_chunks": [], "current_result": {}, "round_index": 0,
                "judge_attempts": 0, "item_error": "", "countercheck": False, "phase": "checking", "stage": 2,
                "message": f"逐项核查 {len(checked)} / {len(state['report']['requirements'])} 项"}

    def document_check(state):
        try:
            result = engine.check_document(state["report"], state["current_requirement"])
            return {"current_result": result, "item_error": ""}
        except ReviewFatalError:
            raise
        except Exception:
            return {"item_error": "文件名称核对失败，已保存为待人工复核，可稍后仅重试该条。", "current_result": {}}

    def retrieve(state):
        requirement = state["current_requirement"]
        try:
            previous = state.get("current_chunks", [])
            initial = state.get("current_result", {})
            options = {}
            if state.get("countercheck"):
                options = {"countercheck": True, "cited_text": "\n".join(
                    str(row.get("quote", "")) for row in initial.get("evidence", []) if row.get("kind") == "支持"
                )}
            chunks = engine.retrieve(state["report"], requirement, state["round_index"], previous, initial.get("gaps", ""), **options)
            if not chunks:
                return {"current_chunks": [], "retrieval_changed": False, "judge_attempts": 0,
                        "current_result": {"status": "证据不足", "project_plan": "未取得可核对的项目方案或实现描述。",
                                           "analysis": "检索范围内没有足够证据，不能据此认定未完成。", "gaps": "缺少对应参数、适用条件或实现描述。",
                                           "recommendation": "补充材料或调整指标条件后复核。", "evidence": []}}
            return {"current_chunks": chunks, "retrieval_changed": chunks != previous, "judge_attempts": 0,
                    "message": f"{requirement['id']} · 已保留 {count_sources(chunks)} 个源切片的原文。"}
        except ReviewFatalError:
            raise
        except Exception:
            return {"item_error": "本条检索失败，已保留其他成功条目，可稍后重试。"}

    def judge(state):
        attempts = state.get("judge_attempts", 0) + 1
        try:
            result = engine.judge(state["report"], state["current_requirement"], state["current_chunks"])
            return {"current_result": result, "judge_ok": True, "judge_attempts": attempts, "item_error": ""}
        except ReviewFatalError:
            raise
        except Exception:
            return {"judge_ok": False, "judge_attempts": attempts, "item_error": "本条判定的结构或来源检查未通过。"}

    def after_retrieval(state):
        if state.get("item_error"):
            return "item_failure"
        if not state.get("current_chunks") or (state["round_index"] and not state.get("retrieval_changed") and state.get("current_result")):
            return "assess_evidence"
        return "judge_numeric" if state["current_requirement"]["type"] == "numeric" else "judge_functional"

    def after_judgment(state):
        if state.get("judge_ok"):
            return "assess_evidence"
        if state["judge_attempts"] < MAX_MODEL_ATTEMPTS:
            return "judge_numeric" if state["current_requirement"]["type"] == "numeric" else "judge_functional"
        return "item_failure"

    def assess_evidence(state):
        return {"item_error": ""}

    def evidence_route(state):
        status = state.get("current_result", {}).get("status")
        if status != "不满足" and state["round_index"] + 1 < MAX_RETRIEVAL_ROUNDS:
            return "supplement_retrieval"
        return "save_item"

    def supplement_retrieval(state):
        countercheck = state.get("current_result", {}).get("status") == "已满足"
        message = "初判已满足，补查同对象的复测、异常及变更记录。" if countercheck else "结合证据缺口补充检索（最多两轮）。"
        return {"round_index": state["round_index"] + 1, "judge_attempts": 0, "item_error": "",
                "countercheck": countercheck, "message": state["current_requirement"]["id"] + " · " + message}

    def item_failure(state):
        previous = deepcopy(state.get("current_result", {}))
        result = {"status": "待人工复核", "project_plan": "本项未能完成有效核查。", "analysis": "本条处理出现异常，不影响其他成功条目。",
                  "gaps": state.get("item_error") or "缺少有效判定。", "recommendation": "检查模型或资料后，使用重试异常项继续。", "evidence": [], **previous,
                  "retryable": True}
        if state.get("countercheck"):
            result.update(status="待人工复核", analysis="首轮初判已满足，未完成通过前反证核查。",
                          gaps=state.get("item_error") or "通过前补查未完成。")
        elif not previous:
            result["status"] = "待人工复核"
        return {"current_result": result}

    def save_item(state):
        report = _report(state)
        identifier = state["current_requirement"]["id"]
        result = {**state["current_result"], "requirementId": identifier, "checkType": state["current_requirement"]["type"],
                  "retrievalRounds": 0 if state["current_requirement"]["type"] == "document" else state["round_index"] + 1,
                  "retrievedChunks": count_sources(state.get("current_chunks", []))}
        by_id = {item["requirementId"]: item for item in report.get("results", [])}
        by_id[identifier] = result
        report["results"] = [by_id[item["id"]] for item in report["requirements"] if item["id"] in by_id]
        _clear_item_issue(report, identifier)
        if result.get("retryable"):
            _issue(report, "指标检索判定", "本条存在处理或有效引用异常，完成的其他条目已保留。", requirementId=identifier, retained=len(result.get("evidence", [])))
        return {"report": report, "checked_ids": list(dict.fromkeys([*state.get("checked_ids", []), identifier])),
                "current_chunks": [], "current_result": {}, "item_error": ""}

    def checking_done(state):
        return {"phase": "reporting", "stage": 3, "message": "逐项核查已保存，开始核对版本与整理报告。"}

    def count_sources(chunks):
        return len({(row.get("document_id"), row.get("id") or row.get("reviewId") or index)
                    for index, row in enumerate(chunks)})

    check = StateGraph(ReviewState)
    functions = {"select_requirement": select_requirement, "document_check": document_check, "numeric_retrieve": retrieve,
                 "functional_retrieve": retrieve, "judge_numeric": judge, "judge_functional": judge, "assess_evidence": assess_evidence,
                 "supplement_retrieval": supplement_retrieval, "item_failure": item_failure, "save_item": save_item, "checking_done": checking_done}
    for name, function in functions.items():
        check.add_node(name, guarded(name, function))
    check.add_edge(START, "select_requirement")
    check.add_conditional_edges("select_requirement", lambda state: {"document": "document_check", "numeric": "numeric_retrieve", "functional": "functional_retrieve"}.get(state["current_requirement"].get("type"), "checking_done"), ["document_check", "numeric_retrieve", "functional_retrieve", "checking_done"])
    check.add_conditional_edges("document_check", lambda state: "item_failure" if state.get("item_error") else "save_item", ["item_failure", "save_item"])
    for node in ("numeric_retrieve", "functional_retrieve"):
        check.add_conditional_edges(node, after_retrieval, ["item_failure", "assess_evidence", "judge_numeric", "judge_functional"])
    for node in ("judge_numeric", "judge_functional"):
        check.add_conditional_edges(node, after_judgment, ["assess_evidence", "judge_numeric", "judge_functional", "item_failure"])
    check.add_conditional_edges("assess_evidence", evidence_route, ["supplement_retrieval", "save_item"])
    check.add_conditional_edges("supplement_retrieval", lambda state: "numeric_retrieve" if state["current_requirement"]["type"] == "numeric" else "functional_retrieve", ["numeric_retrieve", "functional_retrieve"])
    check.add_edge("item_failure", "save_item")
    check.add_edge("save_item", "select_requirement")
    check.add_edge("checking_done", END)

    def collect_report(state):
        assert_versions(state)
        report = _report(state)
        by_id = {item["requirementId"]: item for item in report.get("results", [])}
        report["rawReport"] = [{"id": item["id"], "type": item["type"], "category": item.get("category", ""),
                                "question": item.get("question", ""), "criterion": item["criterion"],
                                "answer": by_id.get(item["id"], {"status": "未回答"})} for item in report["requirements"]]
        report["rawComplete"] = True
        groups, start, size = [], 0, 0
        for index, item in enumerate(report["rawReport"]):
            length = len(json.dumps(item, ensure_ascii=False))
            if size and size + length > 22000:
                groups.append({"start": start, "end": index, "key": _digest(report["rawReport"][start:index])})
                start, size = index, 0
            size += length
        if report["rawReport"]:
            groups.append({"start": start, "end": len(report["rawReport"]), "key": _digest(report["rawReport"][start:])})
        return {"report": report, "report_groups": groups, "report_cursor": 0, "polish_failed": [], "polish_attempts": 0,
                "phase": "reporting", "stage": 3, "message": "固定原始判定与引文，按批次整理展示内容。"}

    def polish_one(state):
        group = state["report_groups"][state["report_cursor"]]
        cache = dict(state.get("polish_cache", {}))
        if group["key"] in cache:
            return {"polish_ok": True}
        attempts = state.get("polish_attempts", 0) + 1
        try:
            rows = state["report"]["rawReport"][group["start"]:group["end"]]
            cache[group["key"]] = engine.polish_batch(state["report"], rows)
            return {"polish_cache": cache, "polish_ok": True, "polish_attempts": attempts}
        except ReviewFatalError:
            raise
        except Exception:
            return {"polish_ok": False, "polish_attempts": attempts}

    def polish_fallback(state):
        group = state["report_groups"][state["report_cursor"]]
        report = _report(state)
        _issue(report, "网页整理", "本批展示整理未通过校验；使用原始分析展示，原判定和证据不变。", batch=group["key"])
        return {"report": report, "polish_failed": list(dict.fromkeys([*state.get("polish_failed", []), group["key"]]))}

    def advance_report(state):
        return {"report_cursor": state["report_cursor"] + 1, "polish_attempts": 0}

    def finalize_report(state):
        assert_versions(state)
        report = _report(state)
        cache = state.get("polish_cache", {})
        sections = []
        for group in state.get("report_groups", []):
            if group["key"] in cache:
                sections.append(cache[group["key"]])
            else:
                sections.append([{"id": item["id"], "status": item["answer"]["status"], "headline": item.get("question", ""),
                                  "summary": item["answer"].get("analysis", ""), "action": item["answer"].get("recommendation", "人工复核")}
                                 for item in report["rawReport"][group["start"]:group["end"]]])
        report.update(polished=sections, complete=True, awaitingConfirmation=False, finishedAt=now(),
                      limited=bool(report.get("issues") or report.get("skippedFiles")))
        retry = bool(state.get("polish_failed") or any(item.get("retryable") for item in report.get("results", [])))
        report["workflowCanRetry"] = retry
        return {"report": report, "retry_available": retry, "phase": "completed", "stage": 3,
                "message": "报告已保存，部分异常可单独重试。" if retry else "审查完成，报告与执行记录已保存。"}

    reporting = StateGraph(ReviewState)
    for name, function in {"collect_report": collect_report, "polish_one": polish_one, "polish_fallback": polish_fallback,
                           "advance_report": advance_report, "finalize_report": finalize_report}.items():
        reporting.add_node(name, guarded(name, function))
    reporting.add_edge(START, "collect_report")
    reporting.add_conditional_edges("collect_report", lambda state: "polish_one" if state["report_groups"] else "finalize_report", ["polish_one", "finalize_report"])
    reporting.add_conditional_edges("polish_one", lambda state: "advance_report" if state["polish_ok"] else "polish_one" if state["polish_attempts"] < MAX_MODEL_ATTEMPTS else "polish_fallback", ["advance_report", "polish_one", "polish_fallback"])
    reporting.add_edge("polish_fallback", "advance_report")
    reporting.add_conditional_edges("advance_report", lambda state: "polish_one" if state["report_cursor"] < len(state["report_groups"]) else "finalize_report", ["polish_one", "finalize_report"])
    reporting.add_edge("finalize_report", END)

    def await_retry(state):
        response = interrupt({"type": "retry_optional", "task_id": state["task_id"],
                              "message": "报告已完成，可选择仅重试异常条目或展示整理。"})
        return {"retry_requested": isinstance(response, dict) and response.get("retry") is True}

    def restart_retry(state):
        assert_versions(state)
        report = _report(state)
        retry_ids = {item["requirementId"] for item in report.get("results", []) if item.get("retryable")}
        for identifier in retry_ids:
            _clear_item_issue(report, identifier)
        report["issues"] = [item for item in report.get("issues", []) if item.get("phase") != "网页整理"]
        report.update(complete=False, polished=[], rawComplete=False, workflowCanRetry=False)
        return {"report": report, "checked_ids": [item["id"] for item in report["requirements"] if item["id"] not in retry_ids],
                "retry_requested": False, "retry_available": False, "polish_failed": [], "phase": "checking", "stage": 2,
                "message": f"仅重试 {len(retry_ids)} 个异常条目，成功条目及可复用的报告批次保持。"}

    graph = StateGraph(ReviewState)
    graph.add_node("initialize", guarded("initialize", initialize))
    graph.add_node("extract_requirements", extract.compile())
    graph.add_node("await_confirmation", guarded("await_confirmation", await_confirmation))
    graph.add_node("validate_confirmation", guarded("validate_confirmation", validate_confirmation))
    graph.add_node("check_requirements", check.compile())
    graph.add_node("report_workflow", reporting.compile())
    graph.add_node("await_retry", guarded("await_retry", await_retry))
    graph.add_node("restart_retry", guarded("restart_retry", restart_retry))
    graph.add_edge(START, "initialize")
    graph.add_edge("initialize", "extract_requirements")
    graph.add_conditional_edges("extract_requirements", lambda state: END if state.get("no_requirements") else "await_confirmation", [END, "await_confirmation"])
    graph.add_edge("await_confirmation", "validate_confirmation")
    graph.add_edge("validate_confirmation", "check_requirements")
    graph.add_edge("check_requirements", "report_workflow")
    graph.add_conditional_edges("report_workflow", lambda state: "await_retry" if state.get("retry_available") else END, ["await_retry", END])
    graph.add_conditional_edges("await_retry", lambda state: "restart_retry" if state.get("retry_requested") else END, ["restart_retry", END])
    graph.add_edge("restart_retry", "check_requirements")
    return graph.compile(checkpointer=checkpointer)
