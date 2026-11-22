from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import secrets

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    data_dir: Path
    frontend_dir: Path = PROJECT_ROOT / "frontend"
    max_upload_mb: int = 64
    secure_cookie: bool = False
    session_hours: int = 12
    max_document_characters: int = 2_000_000
    libreoffice_path: str = ""

    @classmethod
    def from_env(cls):
        load_dotenv(PROJECT_ROOT / ".env")
        if os.getenv("APP_ENABLE_TRACING", "false").lower() != "true":
            os.environ["LANGSMITH_TRACING"] = "false"
            os.environ["LANGCHAIN_TRACING_V2"] = "false"
        directory = Path(os.getenv("APP_DATA_DIR", ".local-data"))
        if not directory.is_absolute():
            directory = PROJECT_ROOT / directory
        return cls(
            data_dir=directory.resolve(),
            max_upload_mb=int(os.getenv("MAX_UPLOAD_MB", "64")),
            secure_cookie=os.getenv("APP_SECURE_COOKIE", "false").lower() == "true",
            libreoffice_path=os.getenv("LIBREOFFICE_PATH", ""),
        )

    def initialize(self):
        self.data_dir = self.data_dir.resolve()
        for part in ("uploads", "previews"):
            (self.data_dir / part).mkdir(parents=True, exist_ok=True)
        secret_file = self.data_dir / "session.secret"
        try:
            with secret_file.open("x", encoding="ascii") as output:
                output.write(secrets.token_urlsafe(48))
            secret_file.chmod(0o600)
        except FileExistsError:
            pass
        return secret_file.read_text(encoding="ascii").strip()
