from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time

from fastapi import HTTPException, Request


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return "pbkdf2_sha256$600000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password, encoded):
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(digest, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


def require_user(request: Request):
    identifier = request.session.get("user_id")
    if not identifier or not request.app.state.db.one("SELECT id FROM users WHERE id=?", (identifier,)):
        raise HTTPException(401, "请先登录")
    return identifier


class LoginLimiter:
    def __init__(self):
        self.attempts = {}
        self.lock = threading.Lock()

    def check(self, address):
        with self.lock:
            cutoff = time.monotonic() - 300
            recent = [item for item in self.attempts.get(address, []) if item > cutoff]
            self.attempts[address] = recent
            if len(recent) >= 10:
                raise HTTPException(429, "登录尝试过于频繁，请五分钟后重试")

    def failed(self, address):
        with self.lock:
            self.attempts.setdefault(address, []).append(time.monotonic())

    def clear(self, address):
        with self.lock:
            self.attempts.pop(address, None)
