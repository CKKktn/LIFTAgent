"""Run a disposable UI verification workspace with a clearly marked fake model.

Used only for browser integration testing. No real contract data or real API
credentials are loaded. The normal start command does not use this fixture.
"""
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from backend.config import Settings
from backend.main import create_app
from backend.security import hash_password
from backend.db import uid, now
from fake_model_provider import FakeProvider
import uvicorn


def main():
    with FakeProvider() as provider, tempfile.TemporaryDirectory(prefix="lift-ui-test-") as temporary:
        app = create_app(Settings(data_dir=Path(temporary)))
        db, knowledge = app.state.db, app.state.knowledge
        db.execute("INSERT INTO users(id,username,password_hash,created_at) VALUES (?,?,?,?)", (uid(), "ui-test", hash_password("ui-test-only-password"), now()))
        app.state.models.save({"name": "联调测试模型（固定响应）", "instance_name": "仅供界面测试", "model_type": "chat", "base_url": provider.base_url, "api_key": "test-fixture", "set_default": True})
        dataset = db.json_list("datasets")[0]
        documents = []
        for name, text in [
            ("技术协议.txt", "项目设备技术协议\n设备续航时间不得低于8小时。\n应提交设备测试报告。"),
            ("项目方案.txt", "项目方案：设备采用双电池，设备续航时间为10小时。支持远程监控和故障告警。"),
            ("测试报告.txt", "设备测试报告：本文件作为测试记录示例，实际测试仍需人工核实。"),
        ]:
            file_path = Path(temporary) / "uploads" / (uid() + ".txt")
            file_path.write_text(text, encoding="utf-8")
            document = knowledge.add_document(dataset["id"], name, file_path, file_path.stat().st_size)
            documents.append(document)
        knowledge.parse([document["id"] for document in documents], dataset["id"])
        import time
        deadline = time.monotonic() + 10
        while any(row["run"] == "RUNNING" for row in db.rows("SELECT run FROM documents")):
            if time.monotonic() > deadline:
                raise RuntimeError("UI fixture could not be parsed")
            time.sleep(0.05)
        print("Disposable UI test workspace: http://127.0.0.1:8001/login", flush=True)
        print("Test account: ui-test / ui-test-only-password", flush=True)
        print("All model responses here are fixed TEST FIXTURES, not a real LLM review.", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=8001, log_level="warning")


if __name__ == "__main__":
    main()
