"""Install the pinned local environment when needed, then run the real app."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def probe(command):
    try:
        result = subprocess.run([*command, "-c", "import json,sys; print(json.dumps([list(sys.version_info[:2]),sys.executable]))"], capture_output=True, text=True, timeout=15)
        version, executable = json.loads(result.stdout.strip())
        return executable if result.returncode == 0 and version == [3, 12] else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def find_python():
    candidates = [[sys.executable]]
    if os.getenv("CONTRACT_PYTHON"):
        candidates.insert(0, [os.environ["CONTRACT_PYTHON"]])
    if shutil.which("py"):
        candidates.append(["py", "-3.12"])
    candidates.extend([[name] for name in ("python3.12", "python3", "python") if shutil.which(name)])
    for candidate in candidates:
        executable = probe(candidate)
        if executable:
            return executable
    raise RuntimeError("Python 3.12 is required. Install Python 3.12 or set CONTRACT_PYTHON to its executable path.")


def dependencies_ready():
    check = """
import importlib.metadata as m, pathlib, re, sys
for line in pathlib.Path(sys.argv[1]).read_text(encoding='utf-8').splitlines():
    match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([^ ;]+)', line.strip())
    if match:
        try:
            if m.version(match[1]) != match[2]: sys.exit(1)
        except m.PackageNotFoundError: sys.exit(1)
"""
    result = subprocess.run([str(PYTHON), "-c", check, str(ROOT / "requirements.lock.txt")], capture_output=True, timeout=20)
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check an existing installation without starting or changing it")
    args = parser.parse_args()
    os.chdir(ROOT)
    if not PYTHON.is_file():
        if args.check:
            raise RuntimeError("Local environment is missing. Run start.cmd to install it.")
        executable = find_python()
        print("Creating the Python 3.12 environment...", flush=True)
        subprocess.run([executable, "-m", "venv", str(VENV)], check=True)
    if not probe([str(PYTHON)]):
        raise RuntimeError("The existing .venv is not Python 3.12. Rename it before creating the supported environment.")
    if not dependencies_ready():
        if args.check:
            raise RuntimeError("Dependencies differ from requirements.lock.txt. Run start.cmd to install them.")
        print("Installing pinned dependencies from PyPI. This is only needed on first use or after a version change.", flush=True)
        subprocess.run([str(PYTHON), "-m", "pip", "install", "-r", str(ROOT / "requirements.lock.txt")], check=True)
    if not (ROOT / "frontend/vendor/pdfjs/cmaps/UniGB-UCS2-H.bcmap").is_file():
        raise RuntimeError("PDF.js font assets are missing. Extract the complete application archive again.")
    if args.check:
        print("Ready: Python 3.12, pinned dependencies, frontend and Chinese PDF fonts.")
        return 0
    return subprocess.call([str(PYTHON), "-m", "backend.main"])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print("Startup failed: " + str(error), file=sys.stderr)
        raise SystemExit(1)
