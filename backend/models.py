from __future__ import annotations

import base64
from collections import Counter
import hashlib
from io import BytesIO
import json
import math
import os
import re
from secrets import randbelow
from urllib.parse import unquote, urlsplit

from cryptography.fernet import Fernet
from fastapi import HTTPException
from langchain_core.embeddings import Embeddings
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from .db import Database, dumps, now, uid


MODEL_TYPES = {"chat": "对话模型", "vision": "视觉模型", "embedding": "嵌入模型", "rerank": "重排模型"}


def vision_connection_probe():
    from PIL import Image, ImageDraw, ImageFont

    digits = f"{randbelow(1_000_000):06d}"
    with Image.new("RGB", (90, 24), "white") as image, BytesIO() as buffer:
        ImageDraw.Draw(image).text((4, 3), digits, font=ImageFont.load_default(), fill="black")
        with image.resize((450, 120), Image.Resampling.NEAREST) as enlarged:
            enlarged.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    message = HumanMessage(content=[
        {"type": "text", "text": "请读取这张合成测试图片中的六位数字，只回复图片里的数字。"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}},
    ])
    return message, digits


def terms(text):
    text = text.casefold()
    tokens = re.findall(r"[a-z0-9]+(?:\.[0-9]+)?|[\u3400-\u9fff]", text)
    for group in re.findall(r"[\u3400-\u9fff]{2,}", text):
        tokens.extend(group[index:index + 2] for index in range(len(group) - 1))
    return tokens


class LocalFeatureEmbeddings(Embeddings):
    """Deterministic local text features, explicitly not a semantic LLM model."""

    dimensions = 384

    def embed_documents(self, texts):
        result = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token, count in Counter(terms(text)).items():
                digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "little") % self.dimensions
                vector[index] += (1 + math.log(count)) * (1 if digest[4] & 1 else -1)
            length = math.sqrt(sum(value * value for value in vector)) or 1
            result.append([value / length for value in vector])
        return result

    def embed_query(self, text):
        return self.embed_documents([text])[0]


def validate_model_address(base):
    if any(character in unquote(base) for character in "{}<>"):
        raise HTTPException(400, "接口地址仍含有 {WorkspaceId} 等占位符，请从模型平台复制完整的实际 API 地址")
    try:
        parsed = urlsplit(base)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname
            and not parsed.username and not parsed.password and not parsed.query
            and not parsed.fragment and not any(character.isspace() for character in base)
            and "\\" not in base)
        parsed.port  # Raises for malformed or out-of-range ports.
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(400, "模型地址必须是完整的 http(s) API 地址，不能包含口令、查询参数或无效端口")
    return base


class ModelRegistry:
    def __init__(self, db: Database, secret):
        self.db = db
        self.cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))

    def all(self):
        values = [{
            "id": "local-features", "model_id": "local-features", "name": "本地文本特征（无需密钥）",
            "model_type": "embedding", "instance_name": "内置", "provider_name": "Local",
            "base_url": "", "api_key_set": False, "read_only": True,
        }, {
            "id": "local-rerank", "model_id": "local-rerank", "name": "关键词与向量混合排序",
            "model_type": "rerank", "instance_name": "内置", "provider_name": "Local",
            "base_url": "", "api_key_set": False, "read_only": True,
        }]
        for kind, prefix in [("chat", "LLM"), ("embedding", "EMBEDDING")]:
            if os.getenv(prefix + "_MODEL") and os.getenv(prefix + "_BASE_URL"):
                values.append({"id": "env-" + kind, "model_id": "env-" + kind,
                    "name": os.environ[prefix + "_MODEL"], "model_type": kind,
                    "instance_name": "环境配置", "provider_name": "OpenAI-Compatible",
                    "base_url": os.environ[prefix + "_BASE_URL"].rstrip("/"),
                    "api_key_set": bool(os.getenv(prefix + "_API_KEY")), "read_only": True})
        for model in self.db.json_list("models"):
            row = self.db.one("SELECT secret FROM models WHERE id=?", (model["id"],))
            values.append({**model, "model_id": model["id"], "api_key_set": bool(row["secret"]), "read_only": False})
        return values

    def default_id(self, kind):
        stored = self.db.one("SELECT model_id FROM model_defaults WHERE kind=?", (kind,))
        options = [item for item in self.all() if item["model_type"] == kind]
        if stored and any(item["id"] == stored["model_id"] for item in options):
            return stored["model_id"]
        return next((item["id"] for item in options if item["id"].startswith("env-")), options[0]["id"] if options else "")

    def find(self, identifier="", kind="chat", private=False):
        identifier = identifier or self.default_id(kind)
        model = next((item.copy() for item in self.all() if item["model_type"] == kind and identifier in (
            item["id"], item["name"], f"{item['name']}@{item['instance_name']}@{item['provider_name']}")), None)
        if model is None:
            raise HTTPException(503, "请先在“模型设置”中配置" + MODEL_TYPES[kind])
        if private:
            if not model["read_only"] or model["id"].startswith("env-"):
                validate_model_address(model["base_url"])
            if model["id"].startswith("env-"):
                model["api_key"] = os.getenv(("LLM" if kind == "chat" else "EMBEDDING") + "_API_KEY", "")
            elif not model["read_only"]:
                secret = self.db.one("SELECT secret FROM models WHERE id=?", (model["id"],))["secret"]
                model["api_key"] = self.cipher.decrypt(secret.encode()).decode() if secret else ""
        return model

    def defaults(self):
        output = []
        saved = {row["kind"]: row["model_id"] for row in self.db.rows("SELECT kind,model_id FROM model_defaults")}
        for kind in MODEL_TYPES:
            identifier = self.default_id(kind)
            if identifier:
                model = self.find(identifier, kind)
                output.append({"model_type": kind, "model_id": identifier, "model_name": model["name"], "enable": True,
                    "configured": saved.get(kind) == identifier or model["read_only"]})
        return output

    def set_default(self, kind, identifier):
        identifier = self.find(identifier, kind)["id"]
        self.db.execute("INSERT INTO model_defaults(kind,model_id) VALUES (?,?) ON CONFLICT(kind) DO UPDATE SET model_id=excluded.model_id", (kind, identifier))

    def save(self, body):
        name = str(body.get("name", "")).strip()
        kind = body.get("model_type", "chat")
        base = str(body.get("base_url", "")).strip().rstrip("/")
        if not name or len(name) > 250 or kind not in MODEL_TYPES:
            raise HTTPException(400, "请填写有效的模型名称和类型")
        validate_model_address(base)
        identifier = str(body.get("id") or uid())
        existing = self.db.one("SELECT secret,payload FROM models WHERE id=?", (identifier,))
        if (identifier.startswith("env-") or identifier in {"local-features", "local-rerank"}):
            raise HTTPException(400, "内置或环境配置不能在这里覆盖")
        previous = json.loads(existing["payload"]) if existing else None
        if previous and previous["model_type"] != kind:
            raise HTTPException(400, "编辑已有接入时不能更改用途，请新建另一项接入")
        model = {"id": identifier, "model_id": identifier, "name": name, "model_type": kind,
            "instance_name": str(body.get("instance_name") or "通用接口")[:100],
            "provider_name": "OpenAI-Compatible", "base_url": base, "updated_at": now()}
        secret = existing["secret"] if existing else ""
        if body.get("clear_api_key"):
            secret = ""
        if str(body.get("api_key") or "").strip():
            secret = self.cipher.encrypt(str(body["api_key"]).strip().encode()).decode()
        reparse = bool(previous and kind in {"embedding", "vision", "chat"}
                       and any(previous[field] != model[field] for field in ("name", "base_url")))
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO models(id,payload,secret) VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,secret=excluded.secret", (identifier, dumps(model), secret))
            if reparse:
                for row in connection.execute("SELECT id,payload FROM datasets").fetchall():
                    dataset = json.loads(row["payload"])
                    if kind == "embedding" and dataset.get("embedding_model") == identifier:
                        connection.execute("UPDATE documents SET run='UNSTART',generation=generation+1,error='嵌入模型接入变更，请重新解析' WHERE dataset_id=?", (row["id"],))
                    elif kind in {"chat", "vision"} and dataset.get("parser_config", {}).get("drawing_vision_model") == identifier:
                        connection.execute("UPDATE documents SET run='UNSTART',generation=generation+1,error='图纸识别模型接入变更，请重新解析' WHERE dataset_id=?", (row["id"],))
        if not self.default_id(kind) or body.get("set_default"):
            self.set_default(kind, identifier)
        return {**self.find(identifier, kind), "requires_reparse": reparse}

    def chat(self, identifier="", temperature=0.1, max_tokens=4096):
        model = self.find(identifier, "chat", private=True)
        return self._chat_client(model, temperature, max_tokens)

    def vision_config(self, identifier="", private=False):
        # Existing drawing knowledge bases store a chat connection's ID.
        legacy = identifier and any(
            item["model_type"] == "chat" and identifier in (
                item["id"], item["name"], f"{item['name']}@{item['instance_name']}@{item['provider_name']}",
            ) for item in self.all()
        )
        return self.find(identifier, "chat" if legacy else "vision", private=private)

    def vision(self, identifier="", temperature=0.1, max_tokens=4096):
        model = self.vision_config(identifier, private=True)
        return self._chat_client(model, temperature, max_tokens)

    @staticmethod
    def _chat_client(model, temperature, max_tokens):
        return ChatOpenAI(
            model=model["name"], base_url=model["base_url"], api_key=model.get("api_key") or "not-required",
            temperature=max(0, min(float(temperature), 2)), max_tokens=max(128, min(int(max_tokens), 16384)),
            timeout=120, max_retries=1, streaming=True, use_responses_api=False,
        )

    def embedding(self, identifier=""):
        model = self.find(identifier, "embedding", private=True)
        if model["id"] == "local-features":
            return "local-features-v1", LocalFeatureEmbeddings()
        fingerprint = hashlib.sha256((model["id"] + "|" + model["name"] + "|" + model["base_url"]).encode()).hexdigest()[:20]
        # Both Qwen 3.7 text embedding variants accept at most 20 texts per request.
        limited_models = {"qwen3.7-text-embedding", "qwen3.7-text-embedding-flash"}
        batch_size = 20 if model["name"].casefold() in limited_models else 32
        return fingerprint, OpenAIEmbeddings(model=model["name"], base_url=model["base_url"],
            api_key=model.get("api_key") or "not-required", check_embedding_ctx_length=False,
            chunk_size=batch_size, request_timeout=60, max_retries=1)


def provider_error(error):
    status = getattr(error, "status_code", None)
    if isinstance(error, HTTPException):
        return str(error.detail)
    if status:
        return f"模型服务返回 HTTP {status}，请检查模型名称、地址、密钥和额度。"
    if "timeout" in type(error).__name__.lower():
        return "模型响应超时，请稍后重试或检查服务状态。"
    if "connection" in type(error).__name__.lower() or "connecterror" in type(error).__name__.lower():
        return "无法连接模型服务，请检查接口地址是否完整，以及运行本系统的电脑能否访问该服务；如使用代理，也请检查代理连接。"
    return f"模型调用失败（{type(error).__name__}），请检查模型设置或后端日志。"
