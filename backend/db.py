from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Database:
    """A connection per operation; explicit transactions protect revision checks."""

    def __init__(self, path: Path):
        self.path = path
        with self.connect() as connection:
            connection.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS datasets (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
                name TEXT NOT NULL, path TEXT NOT NULL, extension TEXT NOT NULL, size INTEGER NOT NULL,
                run TEXT NOT NULL DEFAULT 'UNSTART', enabled INTEGER NOT NULL DEFAULT 1,
                chunk_count INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                generation INTEGER NOT NULL DEFAULT 0, metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS docs_dataset ON documents(dataset_id);
            CREATE TABLE IF NOT EXISTS review_source_paragraphs (
                document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL, source_version TEXT NOT NULL,
                content TEXT NOT NULL, page_number TEXT NOT NULL,
                PRIMARY KEY(document_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
                content TEXT NOT NULL, metadata TEXT NOT NULL, vector TEXT NOT NULL,
                embedding_id TEXT NOT NULL, ordinal INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS chunks_dataset ON chunks(dataset_id, document_id);
            CREATE TABLE IF NOT EXISTS models (id TEXT PRIMARY KEY, payload TEXT NOT NULL, secret TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS model_defaults (kind TEXT PRIMARY KEY, model_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                name TEXT NOT NULL, messages TEXT NOT NULL DEFAULT '[]', revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS annotations (
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                message_index INTEGER NOT NULL, state TEXT NOT NULL, note TEXT NOT NULL,
                revision INTEGER NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(session_id, message_index)
            );
            CREATE TABLE IF NOT EXISTS collab_images (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                media_type TEXT NOT NULL, content BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS review_records (
                id TEXT PRIMARY KEY, report TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
                kb TEXT NOT NULL, kb_name TEXT NOT NULL, contracts TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """)
            connection.execute("UPDATE documents SET run='FAIL', error='服务重启中断了解析，请重新解析', generation=generation+1 WHERE run='RUNNING'")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=20000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    def rows(self, sql, parameters=()):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, parameters).fetchall()]

    def one(self, sql, parameters=()):
        rows = self.rows(sql, parameters)
        return rows[0] if rows else None

    def execute(self, sql, parameters=()):
        with self.connect() as connection:
            cursor = connection.execute(sql, parameters)
            return cursor.rowcount

    def json_get(self, table, identifier):
        assert table in {"datasets", "chats", "models"}
        row = self.one(f"SELECT payload FROM {table} WHERE id=?", (identifier,))
        return json.loads(row["payload"]) if row else None

    def json_list(self, table):
        assert table in {"datasets", "chats", "models"}
        return [json.loads(row["payload"]) for row in self.rows(f"SELECT payload FROM {table} ORDER BY rowid DESC")]

    def json_save(self, table, value):
        assert table in {"datasets", "chats"}
        self.execute(f"INSERT INTO {table}(id,payload) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (value["id"], dumps(value)))
