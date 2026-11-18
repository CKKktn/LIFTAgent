"""Read-only export of knowledge documents and existing chunks from a LIFT site.

Only login and documented GET operations are allowed. Passwords and session
cookies never enter the export. Original files retain their exact bytes.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote, urljoin, urlsplit
import uuid


SECRET_FIELD = re.compile(r"^(?:password|passwd|secret|authorization|cookie|api[_-]?key|access[_-]?token|refresh[_-]?token)$", re.I)


def sanitized(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if SECRET_FIELD.fullmatch(key) else sanitized(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitized(item) for item in value]
    return value


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    partial.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    partial.replace(path)


def digest_file(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def safe_name(value, limit=90):
    result = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip(" .")[:limit]
    if not result or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", result, re.I):
        result = "_" + result
    return result


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ValueError("Unexpected identifier returned by source")
    return value


class ExportError(RuntimeError):
    pass


class SessionExpired(ExportError):
    pass


class SourceClient:
    def __init__(self, base, temporary, timeout=120, max_file_mb=512, interval=.2):
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Use a plain http(s) site address without credentials")
        self.base = base.rstrip("/") + "/"
        self.origin = urlsplit(self.base)[:2]
        self.temporary = Path(temporary)
        self.cookies = self.temporary / "session.cookies"
        self.curl = shutil.which("curl.exe") or shutil.which("curl")
        if not self.curl:
            raise ValueError("curl is required")
        self.timeout, self.max_file_mb, self.interval = timeout, max_file_mb, interval
        self.lock, self.next_request = threading.Lock(), 0.0

    def wait_turn(self):
        with self.lock:
            delay = max(0, self.next_request - time.monotonic())
            if delay:
                time.sleep(delay)
            self.next_request = time.monotonic() + self.interval

    def request(self, path, destination=None, payload=None):
        login = payload is not None
        if login and path != "/auth/login":
            raise ValueError("Only login may use POST")
        if not login and not (path.startswith("/proxy?path=") or path == "/login"):
            raise ValueError("Only documented read-only API paths may be requested")
        current = urljoin(self.base, path.lstrip("/"))
        for attempt in range(3):
            token = uuid.uuid4().hex
            headers_path = self.temporary / (token + ".headers")
            body_path = Path(destination) if destination else self.temporary / (token + ".body")
            self.wait_turn()
            command = [self.curl, "-s", "--show-error", "--compressed", "--connect-timeout", "15", "--max-time", str(self.timeout),
                "--max-filesize", str(self.max_file_mb * 1024 * 1024), "--cookie", str(self.cookies), "--dump-header", str(headers_path),
                "--output", str(body_path), "--write-out", "%{http_code}", "--user-agent", "AuthorizedKnowledgeExport/1.0"]
            if login:
                command += ["--cookie-jar", str(self.cookies), "--request", "POST", "--header", "Content-Type: application/json", "--data-binary", "@-"]
            command.append(current)
            result = subprocess.run(command, input=payload, capture_output=True, timeout=self.timeout + 20)
            headers = {}
            if headers_path.exists():
                for line in headers_path.read_text(encoding="iso-8859-1").splitlines():
                    if line.startswith("HTTP/"):
                        headers = {}
                    elif ":" in line:
                        key, value = line.split(":", 1)
                        headers[key.lower()] = value.strip()
                headers_path.unlink(missing_ok=True)
            status = int(result.stdout.strip() or b"0")
            if status == 401:
                raise SessionExpired(f"Source rejected this session (HTTP {status})")
            if status in {301, 302, 303, 307, 308}:
                # Do not follow storage redirects or send session credentials to another host.
                raise ExportError(f"Source returned HTTP {status}; redirect was not followed")
            if result.returncode or status in {408, 429, 500, 502, 503, 504}:
                if not login and attempt < 2:
                    time.sleep(.5 * (attempt + 1))
                    continue
                raise ExportError(f"Download failed (curl {result.returncode}, HTTP {status})")
            if not 200 <= status < 300:
                raise ExportError(f"Source returned HTTP {status}")
            if destination:
                return headers
            try:
                return json.loads(body_path.read_text(encoding="utf-8-sig"))
            finally:
                body_path.unlink(missing_ok=True)
        raise ExportError("Request did not complete")

    def login(self, username, password):
        result = self.request("/auth/login", payload=json.dumps({"username": username, "password": password}).encode())
        if result.get("success") is False or result.get("code", 0) not in {0, 200} or not self.cookies.is_file():
            raise SessionExpired("Login was not accepted")

    def api(self, path):
        if not re.fullmatch(r"/api/v1/(?:datasets(?:\?.*)?|datasets/[A-Za-z0-9_-]+/documents(?:\?.*)?|datasets/[A-Za-z0-9_-]+/documents/[A-Za-z0-9_-]+/chunks(?:\?.*)?)", path):
            raise ValueError("API operation is outside knowledge export scope")
        result = self.request("/proxy?path=" + quote(path, safe=""))
        if not isinstance(result, dict) or result.get("code", 0) != 0:
            raise ExportError("Knowledge API returned a business error: " + str(result.get("message", "invalid response"))[:250])
        return result.get("data", result)

    def pages(self, base_path, field=None):
        rows, seen, expected = [], set(), None
        for page in range(1, 10001):
            value = self.api(base_path + ("&" if "?" in base_path else "?") + f"page={page}&page_size=100")
            batch = value.get(field, []) if isinstance(value, dict) and field else value
            if not isinstance(batch, list):
                raise ExportError("Unexpected page structure")
            if isinstance(value, dict) and isinstance(value.get("total"), int):
                expected = value["total"]
            added = 0
            for row in batch:
                key = identifier(row.get("id"))
                if key not in seen:
                    rows.append(row)
                    seen.add(key)
                    added += 1
            if not batch or (expected is not None and len(rows) >= expected) or (expected is None and len(batch) < 100):
                break
            if not added:
                raise ExportError("Source repeated a page before reaching its reported total")
        else:
            raise ExportError("Pagination limit reached")
        if expected is not None and len(rows) != expected:
            raise ExportError(f"Source changed during pagination: {len(rows)} of {expected} records")
        return rows

    def original(self, dataset_id, document_id, destination):
        path = "/api/v1/datasets/" + identifier(dataset_id) + "/documents/" + identifier(document_id)
        headers = self.request("/proxy?path=" + quote(path, safe=""), destination=destination)
        if not Path(destination).stat().st_size:
            raise ExportError("Source returned an empty file")
        if "application/json" in headers.get("content-type", "") and Path(destination).stat().st_size < 65536:
            try:
                value = json.loads(Path(destination).read_text(encoding="utf-8-sig"))
            except (ValueError, UnicodeError):
                value = None
            if isinstance(value, dict) and value.get("code", 0) != 0 and value.get("message"):
                raise ExportError("Original file API: " + str(value["message"])[:250])
        return headers


class KnowledgeExport:
    def __init__(self, client, output, workers=2):
        self.client, self.output, self.workers = client, Path(output).resolve(), workers
        self.output.mkdir(parents=True, exist_ok=True)
        manifest = self.output / "export-manifest.json"
        self.manifest = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {"source": client.base, "datasets": []}
        if self.manifest["source"] != client.base:
            raise ValueError("This export directory belongs to another source")
        self.manifest["updated_at"] = datetime.now(timezone.utc).isoformat()

    def relative(self, path):
        return Path(path).relative_to(self.output).as_posix()

    def save(self):
        save_json(self.output / "export-manifest.json", self.manifest)

    def inventory(self):
        old = {row["id"]: row for row in self.manifest["datasets"]}
        datasets = self.client.pages("/api/v1/datasets")
        result = []
        for dataset in datasets:
            key = identifier(dataset["id"])
            previous = old.get(key, {})
            directory = self.output / "datasets" / (key + "__" + safe_name(dataset.get("name", "knowledge"), 24))
            directory.mkdir(parents=True, exist_ok=True)
            save_json(directory / "dataset.json", sanitized(dataset))
            documents = self.client.pages(f"/api/v1/datasets/{key}/documents", "docs")
            save_json(directory / "documents.json", sanitized(documents))
            old_docs = {row["id"]: row for row in previous.get("documents", [])}
            values = []
            for document in documents:
                document_id = identifier(document["id"])
                before = old_docs.get(document_id, {})
                version = [document.get("size"), document.get("chunk_count"), document.get("update_time", document.get("updated_at", document.get("update_date"))), document.get("name")]
                if before.get("source_version") != version:
                    before = {}
                values.append({**before, "id": document_id, "name": document.get("name", document_id),
                    "source_version": version,
                    "source_size": document.get("size"), "source_chunk_count": document.get("chunk_count", 0),
                    "source_run": document.get("run"), "filename_has_replacement_characters": "\ufffd" in document.get("name", "")})
            result.append({"id": key, "name": dataset.get("name", key), "directory": self.relative(directory),
                "source_document_count": dataset.get("document_count"), "source_chunk_count": dataset.get("chunk_count"), "documents": values})
            print(f"Inventory {dataset.get('name',key)}: {len(values)} documents", flush=True)
        self.manifest["datasets"] = result
        self.save()
        return result

    def export_one(self, dataset, document):
        directory = self.output / dataset["directory"]
        result = dict(document)
        key = document["id"]
        chunks_path = directory / "chunks" / (key + ".json")
        text_path = directory / "text" / (key + ".txt")
        original_dir = directory / "originals"
        original_dir.mkdir(exist_ok=True)
        # Names may contain source folder paths or invalid Windows characters.
        base = document["name"].replace("\\", "/").rsplit("/", 1)[-1]
        suffix = Path(base).suffix
        extension = ("." + safe_name(suffix[1:], 14)) if suffix else ""
        filename = key + "__" + safe_name(base[:-len(suffix)] if suffix else base, 60) + extension
        original_path = original_dir / filename
        try:
            if not (result.get("chunks_status") == "ok" and chunks_path.is_file() and text_path.is_file()
                and result.get("chunks_sha256") == digest_file(chunks_path) and result.get("text_sha256") == digest_file(text_path)):
                chunks = self.client.pages(f"/api/v1/datasets/{dataset['id']}/documents/{key}/chunks", "chunks")
                save_json(chunks_path, sanitized(chunks))
                text_path.parent.mkdir(parents=True, exist_ok=True)
                text_path.write_text("\n\n".join(str(row.get("content", row.get("content_with_weight", ""))) for row in chunks), encoding="utf-8")
                result.update(chunks_status="ok", chunk_count=len(chunks), chunks_path=self.relative(chunks_path),
                    chunks_sha256=digest_file(chunks_path), text_path=self.relative(text_path), text_sha256=digest_file(text_path))
            result.pop("chunks_error", None)
        except SessionExpired:
            raise
        except Exception as error:
            result.update(chunks_status="failed", chunks_error=str(error)[:500])
        try:
            if not (result.get("original_status") == "ok" and original_path.is_file() and result.get("original_sha256") == digest_file(original_path)):
                required = int(document.get("source_size") or 0) + 128 * 1024 * 1024
                if shutil.disk_usage(self.output).free < required:
                    raise ExportError("Insufficient free disk space")
                partial = original_path.with_name(original_path.name + ".part")
                try:
                    headers = self.client.original(dataset["id"], key, partial)
                    partial.replace(original_path)
                finally:
                    partial.unlink(missing_ok=True)
                result.update(original_status="ok", original_path=self.relative(original_path), original_bytes=original_path.stat().st_size,
                    original_sha256=digest_file(original_path), content_type=headers.get("content-type", ""))
            result.pop("original_error", None)
        except SessionExpired:
            raise
        except Exception as error:
            result.update(original_status="failed", original_error=str(error)[:500])
        return result

    def export(self, limit=0):
        remaining = limit or None
        for dataset in self.manifest["datasets"]:
            documents = dataset["documents"]
            selected = documents if remaining is None else documents[:remaining]
            if not selected:
                break
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = {pool.submit(self.export_one, dataset, row): index for index, row in enumerate(selected)}
                for done, future in enumerate(as_completed(futures), 1):
                    documents[futures[future]] = future.result()
                    self.save()
                    if done % 10 == 0 or done == len(selected):
                        print(f"Export {dataset['name']}: {done}/{len(selected)}", flush=True)
            self.report()
            if remaining is not None:
                remaining -= len(selected)
        self.report()

    def report(self):
        rows = [row for dataset in self.manifest["datasets"] for row in dataset["documents"]]
        summary = {"source": self.client.base, "datasets": len(self.manifest["datasets"]), "documents": len(rows),
            "originals_saved": sum(row.get("original_status") == "ok" for row in rows),
            "chunk_documents_saved": sum(row.get("chunks_status") == "ok" for row in rows),
            "chunks_saved": sum(row.get("chunk_count", 0) for row in rows if row.get("chunks_status") == "ok"),
            "original_bytes": sum(row.get("original_bytes", 0) for row in rows if row.get("original_status") == "ok"),
            "source_filename_encoding_issues": sum(row.get("filename_has_replacement_characters", False) for row in rows),
            "original_failures": sum(row.get("original_status") == "failed" for row in rows),
            "chunk_failures": sum(row.get("chunks_status") == "failed" for row in rows)}
        save_json(self.output / "summary.json", summary)
        lines = ["# 原站知识库导出", "", f"来源：{self.client.base}", "", "只读取当前账号可访问的数据，没有修改、删除、重新解析原站文件，也没有调用生成模型。", "",
            "| 知识库 | 文件数 | 原件成功 | 解析文字成功 | 切片数 |", "| --- | ---: | ---: | ---: | ---: |"]
        for dataset in self.manifest["datasets"]:
            docs = dataset["documents"]
            lines.append(f"| {dataset['name']} | {len(docs)} | {sum(d.get('original_status')=='ok' for d in docs)} | {sum(d.get('chunks_status')=='ok' for d in docs)} | {sum(d.get('chunk_count',0) for d in docs if d.get('chunks_status')=='ok')} |")
        lines += ["", "每个知识库的 originals/ 保存下载原件，text/ 保存已有解析文字，chunks/ 保留切片与来源信息。export-manifest.json 记录每个文件的来源、结果和 SHA-256。", "",
            "text/ 是原站已有解析内容的整理，不保证覆盖原件全部图表或正文；没有切片不等于原件为空。若原站文件名含替换字符（�），保留原始名称记录，不猜测重命名。", "",
            "这是可访问资料的导出，不是原站数据库或向量索引的完整备份。导入新系统后应重新建立其检索索引。", "",
            f"下载失败 {summary['original_failures']} 份，切片读取失败 {summary['chunk_failures']} 份。详细原因见 export-manifest.json。"]
        (self.output / "导出说明.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.save()
        return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password-env", default="CONTRACT_REVIEW_PASSWORD")
    parser.add_argument("--password-stdin", action="store_true")
    parser.add_argument("--output", required=True)
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--workers", type=int, default=2, choices=[1, 2, 3])
    parser.add_argument("--limit", type=int, default=0, help="Optional pilot export document limit; zero exports all")
    args = parser.parse_args()
    password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else os.getenv(args.password_env) or getpass.getpass("Site password (not saved): ")
    if not password:
        raise ValueError("Password is empty")
    with tempfile.TemporaryDirectory(prefix="knowledge-export-session-") as temporary:
        client = SourceClient(args.base_url, temporary)
        client.login(args.username, password)
        del password
        exporter = KnowledgeExport(client, args.output, args.workers)
        exporter.inventory()
        if not args.inventory_only:
            exporter.export(args.limit)
        print(json.dumps(exporter.report(), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
