from __future__ import annotations

from collections import Counter
import json
import math
from typing import Any

import httpx
from fastapi import HTTPException
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_core.vectorstores import VectorStore

from .db import dumps, uid
from .models import terms


class SQLiteVectorStore(VectorStore):
    """LangChain VectorStore over persisted chunks, with scoped hybrid ranking."""

    def __init__(self, db, embedding, embedding_id, dataset_ids, document_ids=None, exclude_ids=None, vector_weight=0.3):
        self.db, self._embedding, self.embedding_id = db, embedding, embedding_id
        self.dataset_ids, self.document_ids = dataset_ids, document_ids
        self.exclude_ids, self.vector_weight = exclude_ids or [], vector_weight

    @property
    def embeddings(self):
        return self._embedding

    @classmethod
    def from_texts(cls, texts, embedding, metadatas=None, **kwargs):
        store = cls(embedding=embedding, **kwargs)
        store.add_texts(texts, metadatas)
        return store

    def add_texts(self, texts, metadatas=None, **kwargs):
        texts = list(texts)
        metadata = metadatas or [{} for _ in texts]
        vectors = self.embeddings.embed_documents(texts)
        identifiers = []
        with self.db.transaction() as connection:
            for index, (text, meta, vector) in enumerate(zip(texts, metadata, vectors)):
                identifier = uid()
                document_id = meta["document_id"]
                row = connection.execute("SELECT dataset_id FROM documents WHERE id=?", (document_id,)).fetchone()
                if not row or row["dataset_id"] not in self.dataset_ids:
                    raise ValueError("The document does not belong to this vector store")
                connection.execute("INSERT INTO chunks(id,document_id,dataset_id,content,metadata,vector,embedding_id,ordinal) VALUES (?,?,?,?,?,?,?,?)",
                    (identifier, document_id, row["dataset_id"], text, dumps(meta), dumps(vector), self.embedding_id, index))
                connection.execute("UPDATE documents SET chunk_count=(SELECT COUNT(*) FROM chunks WHERE document_id=?),run='DONE' WHERE id=?", (document_id, document_id))
                identifiers.append(identifier)
        return identifiers

    def similarity_search(self, query, k=4, **kwargs):
        return [document for document, _ in self.similarity_search_with_score(query, k=k, **kwargs)]

    def similarity_search_with_score(self, query, k=4, **kwargs):
        if not self.dataset_ids:
            return []
        conditions = ["c.dataset_id IN (" + ",".join("?" for _ in self.dataset_ids) + ")",
            "c.embedding_id=?", "c.enabled=1", "d.enabled=1", "d.run='DONE'"]
        parameters = [*self.dataset_ids, self.embedding_id]
        for identifiers, negate in [(self.document_ids, False), (self.exclude_ids, True)]:
            if identifiers is not None and (identifiers or not negate):
                if not identifiers:
                    return []
                conditions.append("c.document_id " + ("NOT IN" if negate else "IN") + " (" + ",".join("?" for _ in identifiers) + ")")
                parameters.extend(identifiers)
        rows = self.db.rows("SELECT c.*,d.name AS document_name FROM chunks c JOIN documents d ON d.id=c.document_id WHERE " + " AND ".join(conditions), parameters)
        if not rows:
            return []
        query_vector = self.embeddings.embed_query(query)
        query_norm = math.sqrt(sum(value * value for value in query_vector)) or 1
        query_terms = set(terms(query))
        counters = [Counter(terms(row["content"])) for row in rows]
        frequencies = Counter(token for counter in counters for token in counter.keys() if token in query_terms)
        average_length = sum(sum(counter.values()) for counter in counters) / max(1, len(rows))
        scored = []
        for row, counter in zip(rows, counters):
            vector = json.loads(row["vector"])
            if len(vector) != len(query_vector):
                raise ValueError("嵌入向量维度已改变，请重新解析该知识库")
            denominator = math.sqrt(sum(value * value for value in vector)) * query_norm or 1
            cosine = max(0, min(1, sum(a * b for a, b in zip(vector, query_vector)) / denominator))
            bm25 = 0.0
            length = sum(counter.values())
            for token in query_terms:
                count = counter[token]
                if count:
                    idf = math.log(1 + (len(rows) - frequencies[token] + 0.5) / (frequencies[token] + 0.5))
                    bm25 += idf * count * 2.2 / (count + 1.2 * (0.25 + 0.75 * length / max(1, average_length)))
            lexical = bm25 / (bm25 + 1.5)
            score = self.vector_weight * cosine + (1 - self.vector_weight) * lexical
            meta = {**json.loads(row["metadata"]), "id": row["id"], "document_id": row["document_id"],
                "document_name": row["document_name"], "dataset_id": row["dataset_id"], "similarity": score,
                "vector_similarity": cosine, "term_similarity": lexical}
            scored.append((Document(page_content=row["content"], metadata=meta), score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:k]


class KnowledgeRetriever(BaseRetriever):
    service: Any
    options: dict

    def _get_relevant_documents(self, query, *, run_manager):
        result = self.service.search({**self.options, "question": query})
        return [Document(page_content=item["content"], metadata=item) for item in result["chunks"]]


class RetrievalService:
    def __init__(self, db, models, knowledge):
        self.db, self.models, self.knowledge = db, models, knowledge

    def rerank(self, question, documents, model_id=""):
        model = self.models.find(model_id, "rerank", private=True)
        base = model["base_url"].rstrip("/")
        with httpx.Client(timeout=60) as client:
            response = client.post(base if base.endswith("/rerank") else base + "/rerank",
                headers={"Authorization": "Bearer " + (model.get("api_key") or "not-required")},
                json={"model": model["name"], "query": question, "documents": documents, "top_n": len(documents)})
            response.raise_for_status()
            try:
                results = response.json().get("results")
                if not isinstance(results, list) or not results:
                    raise ValueError()
                seen, output = set(), []
                for item in results:
                    if not isinstance(item, dict) or type(item.get("index")) is not int or not 0 <= item["index"] < len(documents) or item["index"] in seen or isinstance(item.get("relevance_score"), bool):
                        raise ValueError()
                    score = float(item["relevance_score"])
                    if not math.isfinite(score):
                        raise ValueError()
                    seen.add(item["index"])
                    output.append({"index": item["index"], "relevance_score": max(0, min(1, score))})
            except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
                raise HTTPException(502, "重排模型返回格式无效，请检查是否使用兼容的 /rerank 接口；未采用该排序结果") from None
        return output

    def search(self, body):
        question = str(body.get("question", "")).strip()
        if not question or len(question) > 16000:
            raise ValueError("检索问题不能为空，且不得超过 16000 字")
        dataset_ids = body.get("dataset_ids") or []
        if not isinstance(dataset_ids, list) or not dataset_ids or len(dataset_ids) > 50:
            raise ValueError("请选择 1–50 个知识库")
        threshold = max(0, min(1, float(body.get("similarity_threshold", 0.2))))
        weight = max(0, min(1, float(body.get("vector_similarity_weight", 0.3))))
        size = max(1, min(100, int(body.get("page_size", 10))))
        page = max(1, int(body.get("page", 1)))
        candidates = []
        for identifier in dict.fromkeys(dataset_ids):
            dataset = self.knowledge.dataset(identifier)
            embedding_id, embedding = self.models.embedding(dataset["embedding_model"])
            store = SQLiteVectorStore(self.db, embedding, embedding_id, [identifier], body.get("document_ids"), body.get("exclude_document_ids"), weight)
            candidates.extend(store.similarity_search_with_score(question, k=max(64, min(1000, page * size))))
        candidates.sort(key=lambda item: item[1], reverse=True)
        candidates = [(document, score) for document, score in candidates if score >= threshold]
        reranker = self.models.find("", "rerank", private=True)
        if candidates and reranker["id"] != "local-rerank":
            selection = candidates[:64]
            results = self.rerank(question, [item[0].page_content for item in selection], reranker["id"])
            candidates = [(selection[item["index"]][0], max(0, min(1, float(item["relevance_score"])))) for item in results]
        chunks = []
        for document, score in candidates[(page - 1) * size:page * size]:
            chunks.append({**document.metadata, "content": document.page_content, "content_with_weight": document.page_content,
                "similarity": score, "positions": []})
        return {"total": len(candidates), "chunks": chunks, "doc_aggs": [
            {"doc_id": identifier, "doc_name": next(item["document_name"] for item in chunks if item["document_id"] == identifier),
                "count": sum(item["document_id"] == identifier for item in chunks)} for identifier in dict.fromkeys(item["document_id"] for item in chunks)]}
