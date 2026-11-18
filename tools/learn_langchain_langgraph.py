r"""Offline learning demo using the project's real splitting and retrieval code.

Run from V4: .\.venv\Scripts\python.exe -X utf8 -u -m tools.learn_langchain_langgraph
The chat model has a fixed response. All documents and database files are synthetic
and temporary; this module does not load .env or open the daily workspace.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
from typing import TypedDict

from langchain_core.documents import Document
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from backend.db import Database, now
from backend.models import LocalFeatureEmbeddings
from backend.parsers import split_documents
from backend.retrieval import KnowledgeRetriever, RetrievalService, SQLiteVectorStore


DATASET_ID = "learning-dataset"
BASELINE_ID = "learning-baseline"
EMBEDDING_ID = "local-features-v1"


class DemoModels:
    """Supply local implementations without consulting saved model settings."""

    def __init__(self):
        self.local_embedding = LocalFeatureEmbeddings()

    def embedding(self, identifier=""):
        return EMBEDDING_ID, self.local_embedding

    def find(self, identifier="", kind="rerank", private=False):
        if kind != "rerank":
            raise ValueError("This demo only supplies the local reranker")
        return {"id": "local-rerank"}


class DemoKnowledge:
    def dataset(self, identifier):
        if identifier != DATASET_ID:
            raise ValueError("Unknown demo dataset")
        return {"id": DATASET_ID, "embedding_model": "local-features"}


class DemoState(TypedDict, total=False):
    question: str
    references: list[dict]
    answer: str


def build_retriever(db):
    models = DemoModels()
    timestamp = now()
    db.json_save("datasets", {"id": DATASET_ID, "name": "合成学习资料"})
    store = SQLiteVectorStore(db, models.local_embedding, EMBEDDING_ID, [DATASET_ID])
    samples = [
        (BASELINE_ID, "合成技术协议.txt", "设备续航时间应不低于8小时。"),
        ("learning-plan", "合成项目方案.txt", "设备采用双电池，项目方案规定设备续航时间为10小时。"),
        ("learning-other", "合成商务说明.txt", "付款方式为分期支付，发票应注明项目名称。"),
    ]
    for identifier, name, text in samples:
        db.execute(
            "INSERT INTO documents(id,dataset_id,name,path,extension,size,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (identifier, DATASET_ID, name, "synthetic-only", ".txt", len(text.encode()), timestamp, timestamp),
        )
        documents = [Document(page_content=text, metadata={"document_id": identifier, "source_kind": "text"})]
        parts = split_documents(documents, "naive", {"chunk_size": 200, "chunk_overlap": 30})
        # add_documents is inherited from LangChain VectorStore and calls our add_texts.
        store.add_documents(parts)
    retrieval = RetrievalService(db, models, DemoKnowledge())
    return KnowledgeRetriever(service=retrieval, options={
        "dataset_ids": [DATASET_ID],
        "exclude_document_ids": [BASELINE_ID],
        "page_size": 2,
        "similarity_threshold": 0.1,
    })


def build_graph(retriever):
    async def retrieve(state):
        documents = await retriever.ainvoke(state["question"])
        return {"references": [{**doc.metadata, "content": doc.page_content} for doc in documents]}

    async def answer(state):
        context = "\n\n".join(
            f"[{index}] 文件：{item['document_name']}\n{item['content']}"
            for index, item in enumerate(state["references"], start=1)
        )
        prompt = ChatPromptTemplate.from_messages([
            ("system", "仅根据材料回答并引用编号。方案参数不等同于实测达标。\n材料：{context}"),
            MessagesPlaceholder("history"),
            ("human", "{question}"),
        ])
        # Fixed text makes streaming and graph behavior observable without an API key.
        model = FakeListChatModel(responses=[
            "项目方案规定设备续航时间为10小时[1]。这是方案参数，不能据此确认实测达标。"
        ])
        chain = prompt | model
        writer = get_stream_writer()
        result = ""
        async for chunk in chain.astream({"context": context, "history": [], "question": state["question"]}):
            delta = chunk.content
            result += delta
            writer({"delta": delta})
        return {"answer": result}

    graph = StateGraph(DemoState)
    graph.add_node("retrieve", retrieve)
    graph.add_node("answer", answer)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "answer")
    graph.add_edge("answer", END)
    return graph.compile()


async def run_demo(directory):
    db = Database(Path(directory) / "learning.sqlite3")
    graph = build_graph(build_retriever(db))
    print("实际执行图：")
    print(graph.get_graph().draw_mermaid())
    initial = {"question": "设备续航时间是多少？", "references": []}
    print("\n初始状态：", initial)
    final = dict(initial)
    async for mode, event in graph.astream(initial, stream_mode=["custom", "updates"]):
        if mode == "updates":
            for node, changes in event.items():
                final.update(changes)
                if node == "retrieve":
                    print("\nretrieve 节点写入 references：")
                    for index, item in enumerate(changes["references"], start=1):
                        print(f"  [{index}] {item['document_name']}，混合分数 {item['similarity']:.3f}")
                        print("     " + item["content"])
                    print("answer 节点通过 custom 事件输出：")
                else:
                    print("\nanswer 节点写入 answer，共", len(changes["answer"]), "个字符。")
        elif mode == "custom":
            print(event["delta"], end="", flush=True)
    assert final["references"], "Synthetic project plan should be retrieved"
    assert final["references"][0]["document_id"] == "learning-plan"
    assert all(item["document_id"] != BASELINE_ID for item in final["references"])
    assert "10小时" in final["answer"]
    print("\n练习检查通过：项目方案已召回，作为依据的技术协议已排除，图已生成完整回答。")


def main():
    # Process-local settings prevent optional tracing from sending learning data.
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGCHAIN_TRACING"] = "false"
    print("离线教学练习：使用真实分块/检索代码和固定响应聊天模型；不验证真实 AI 审查质量。")
    with tempfile.TemporaryDirectory(prefix="contract-langchain-learning-") as directory:
        asyncio.run(run_demo(directory))
    print("临时学习数据库已清理。")


if __name__ == "__main__":
    main()
