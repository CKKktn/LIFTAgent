"""Local protocol fixture ONLY. Production app never imports this module.

The fixed answers verify JSON/SSE plumbing, source mapping and the three-stage
UI workflow; they are not evidence of a real model's review quality.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading
import time


def json_after(text, marker):
    return json.JSONDecoder().raw_decode(text.split(marker, 1)[1].strip())[0]


def answer_for(messages):
    text = str(messages[-1]["content"])
    combined = "\n".join(str(item.get("content", "")) for item in messages)
    if "COLLAB_" in combined and "INPUT_JSON:" in text:
        value = json_after(text, "INPUT_JSON:")
        question = str(value.get("question", ""))
        references = value.get("references", [])
        if "COLLAB_REWRITE_V1" in combined:
            history = value.get("history", [])
            previous = next((item.get("content", "") for item in reversed(history) if item.get("role") == "user"), "")
            standalone = (str(previous) + "；追问：" + question) if previous and re.search(r"^(?:那|这|它|该项)", question) else question
            return json.dumps({"standalone_question": standalone, "intent": "review" if re.search(r"达标|符合|判定|合格|审查结论", question) else "question"}, ensure_ascii=False)
        if "COLLAB_EVIDENCE_V1" in combined:
            sufficient = bool(references) and "NO_SOURCE_TEST" not in question
            return json.dumps({"sufficient": sufficient, "relevant_ids": [item["source_id"] for item in references] if sufficient else [], "missing": [] if sufficient else ["缺少对应原始材料"], "conflicts": [], "summary": "合成证据检查，仅用于验证流程"}, ensure_ascii=False)
        if "COLLAB_VALIDATE_V1" in combined:
            identifiers = [int(item) for item in re.findall(r"\[(\d+)\]", str(value.get("answer", "")))]
            known = {item["source_id"] for item in references}
            grounded = bool(references) and all(item in known for item in identifiers)
            return json.dumps({"grounded": grounded, "issues": [] if grounded else ["证据或来源编号不足"], "supported_ids": [item for item in identifiers if item in known], "summary": "合成回答核对，仅用于软件测试"}, ensure_ascii=False)
        if "COLLAB_ANSWER_V1" in combined:
            if not references:
                return "本轮未检索到可引用的知识库材料，请补充资料后复核。"
            if not value.get("evidence_assessment", {}).get("sufficient", True):
                return "本轮材料不足以确认问题中的实测或验收结论，请补充对应对象、阶段和适用条件的原始材料后复核。"
            source = next((item for item in references if "10小时" in item.get("content", "") or "10 小时" in item.get("content", "")), references[0])
            return "根据项目方案，设备续航为10小时 [" + str(source["source_id"]) + "]。此为方案参数，实际测试与验收仍需核对。"
    if "technical_indicators" in text and "原文段落：" in text:
        rows = json_after(text, "原文段落：")
        numeric = next((item for item in rows if "续航" in item["text"]), rows[0])
        report = next((item for item in rows if "报告" in item["text"]), rows[-1])
        return json.dumps({"technical_indicators": [{"type": "numeric", "subject": "设备续航", "title": "设备续航时间", "criterion": "设备续航时间不得低于8小时", "rationale": "原文明确规定设备性能阈值", "source_ids": [numeric["id"]]}], "closure_conditions": [{"type": "document", "subject": "测试报告", "title": "测试报告", "document_name": "测试报告", "criterion": "应提交设备测试报告", "rationale": "原文规定技术交付物", "source_ids": [report["id"]]}]}, ensure_ascii=False)
    if "原始汇报：" in text and '"sections"' in text:
        rows = json_after(text, "原始汇报：")
        return json.dumps({"sections": [{"id": item["id"], "status": item["answer"]["status"], "headline": "示例条款核查", "summary": item["answer"]["analysis"], "action": item["answer"]["recommendation"]} for item in rows]}, ensure_ascii=False)
    if "证据：" in text and '"project_plan"' in text:
        rows = json_after(text, "证据：")
        source = next((item for item in rows if "10小时" in item.get("content", "")), rows[0])
        return json.dumps({"status": "已满足", "project_plan": "项目方案描述续航为10小时", "analysis": "方案10小时高于协议8小时要求，属于方案符合，不代表已实测达标。", "gaps": "仍需人工核实实际测试记录", "recommendation": "人工复核测试记录与适用条件", "evidence": [{"source_id": source["source_id"], "analysis": "该原文描述方案续航参数", "kind": "支持"}]}, ensure_ascii=False)
    if '"matches"' in text and "文件名：" in text:
        rows = json_after(text, "文件名：")
        return json.dumps({"matches": [{"id": item["id"], "reason": "文件名称包含指定技术交付物"} for item in rows if "测试报告" in item.get("name", "")]}, ensure_ascii=False)
    if "请仅回复 OK" in text:
        return "OK"
    if "NO_SOURCE_TEST" in text:
        return "未检索到该问题的知识库证据，需要补充材料后复核。"
    return "根据提供的项目方案，设备续航为10小时 [1]。这表示方案符合，需要人工核对实测记录。"


class ProviderHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        if self.path.endswith("/embeddings"):
            from backend.models import LocalFeatureEmbeddings
            texts = body["input"]
            if isinstance(texts, str):
                texts = [texts]
            result = {"object": "list", "data": [{"object": "embedding", "index": index, "embedding": vector} for index, vector in enumerate(LocalFeatureEmbeddings().embed_documents(texts))], "model": body["model"], "usage": {"prompt_tokens": 1, "total_tokens": 1}}
            payload = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        answer = answer_for(body["messages"])
        if not body.get("stream"):
            payload = json.dumps({"id": "test-completion", "object": "chat.completion", "created": int(time.time()), "model": body["model"], "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for start in range(0, len(answer), 40):
                event = {"id": "test-completion", "object": "chat.completion.chunk", "created": int(time.time()), "model": body["model"], "choices": [{"index": 0, "delta": {"content": answer[start:start + 40]}, "finish_reason": None}]}
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


class FakeProvider:
    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
