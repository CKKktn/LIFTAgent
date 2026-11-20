"""Build a source-only, self-contained application archive using an allowlist."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent
NAME = "contract-review-langchain-0.5.0"
FILES = ["README.md", "THIRD_PARTY_NOTICES.md", ".env.example", ".gitignore", "pytest.ini", "requirements.txt", "requirements.lock.txt", "start.cmd", "start.ps1", "start-trial.cmd",
    "tools/bootstrap.py", "tools/package_app.py", "tools/run_ui_fixture.py", "tools/install_background_assets.py", "tools/export_review_graph.py",
    "tools/export_knowledge.py", "tools/import_knowledge.py", "tools/recover_knowledge_ocr.py", "tools/windows_ocr.ps1",
    "tests/test_backend.py", "tests/fake_model_provider.py", "tests/test_frontend_state.mjs", "tests/test_answer_output.mjs", "tests/test_requirement_editor.mjs", "tests/test_report_workspace.mjs", "tests/test_report_export.mjs", "tests/test_review_jobs.mjs", "tests/test_workflow_academic.mjs", "tests/test_review_engine.py", "tests/test_review_jobs.py", "tests/test_review_graph.py",
    "tests/test_knowledge_export.py", "tests/test_knowledge_import.py", "tests/test_knowledge_ocr.py", "tests/test_delivery_regressions.py",
    "tests/browser/quick_review_fixture.py", "tests/browser/verify_handoff_ui.py",
    "tests/test_collab_workflow.py", "tests/test_collab_engine.py", "tests/test_collab_jobs.mjs", "tests/browser/verify_collab_ui.py",
    "tests/test_collab_recovery.py", "tests/helpers/review_graph.mjs",
    "tests/test_drawing_evidence.py", "tests/test_drawing_evidence_ui.mjs", "tests/test_drawing_sources.py", "tests/test_visual_models.py"]
DIRECTORIES = ["backend", "frontend", "examples"]


def application_files():
    paths = {ROOT / name for name in FILES}
    for directory in DIRECTORIES:
        paths.update(path for path in (ROOT / directory).rglob("*") if path.is_file()
            and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
            and (directory != "examples" or path.suffix.casefold() != ".md"))
    for path in sorted(paths):
        if not path.is_file() or not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError("Missing or invalid application file: " + str(path))
        yield path


def main():
    destination = ROOT / "dist" / (NAME + ".zip")
    destination.parent.mkdir(exist_ok=True)
    manifest = {}
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in application_files():
            relative = path.relative_to(ROOT).as_posix()
            content = path.read_bytes()
            manifest[relative] = {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
            item = zipfile.ZipInfo(NAME + "/" + relative, date_time=(2026, 10, 28, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o644 << 16
            archive.writestr(item, content)
        item = zipfile.ZipInfo(NAME + "/SHA256SUMS.json", date_time=(2026, 10, 28, 0, 0, 0))
        item.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(item, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    # Verify the completed artifact rather than assuming that writing succeeded.
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise ValueError("Archive integrity check failed")
        for relative, expected in manifest.items():
            content = archive.read(NAME + "/" + relative)
            if hashlib.sha256(content).hexdigest() != expected["sha256"]:
                raise ValueError("Archive content changed: " + relative)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix(".zip.sha256").write_text(digest + "  " + destination.name + "\n", encoding="ascii")
    print(f"Packaged {len(manifest)} source/assets/doc files; {destination.stat().st_size:,} bytes")
    print(destination)
    print("SHA-256: " + digest)


if __name__ == "__main__":
    main()
