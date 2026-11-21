"""Backend setup and health check: `pdf2md setup` installs, `pdf2md check` verifies.

Nothing else in pdf2md installs or downloads anything. Conversions (and the Obsidian plugin) only *use* the
environment that `pdf2md setup` created, and stop with instructions if it is missing.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

WORKER_DEPS = ["paddlepaddle==3.2.1", "paddleocr[doc-parser]==3.7.0", "pypdfium2", "pypdf>=6,<7"]
WORKER_PYTHON = "3.12"
MODEL_REPO = "PaddlePaddle/PaddleOCR-VL-1.6-GGUF"
MODEL_FILES = ("PaddleOCR-VL-1.6-GGUF.gguf", "PaddleOCR-VL-1.6-GGUF-mmproj.gguf")
DEFAULT_SERVER = "http://127.0.0.1:8111/v1"


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(os.environ.get("PDF2MD_HOME") or Path(base) / "pdf2md-local")


def model_dir() -> Path:
    return Path(os.environ.get("MODEL_DIR") or Path.home() / ".cache" / "pdf2md" / "models" / "paddleocr-vl-1.6")


def venv_dir() -> Path:
    return data_dir() / "worker-venv"


def worker_python() -> Path:
    v = venv_dir()
    return v / "Scripts" / "python.exe" if os.name == "nt" else v / "bin" / "python"


SETUP_HINT = "Run `pdf2md setup` once in a terminal, then `pdf2md check`."


def worker_imports_ok() -> bool:
    py = worker_python()
    if not py.is_file():
        return False
    r = subprocess.run([str(py), "-c", "import paddleocr, pypdfium2, pypdf"], capture_output=True)
    return r.returncode == 0


def models_present() -> bool:
    return all((model_dir() / f).is_file() and (model_dir() / f).stat().st_size > 0 for f in MODEL_FILES)


def llama_server() -> str | None:
    cmd = os.environ.get("LLAMA_SERVER", "llama-server")
    return shutil.which(cmd) or (cmd if Path(cmd).is_file() else None)


def server_reachable(url: str = DEFAULT_SERVER, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/models", timeout=timeout):
            return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def gpu_name() -> str | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    r = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True, text=True)
    return r.stdout.strip().splitlines()[0] if r.returncode == 0 and r.stdout.strip() else None


def status(server: str = DEFAULT_SERVER) -> list[dict]:
    """Checks as {id, ok, required, detail, fix}. Required ones decide the exit code of `pdf2md check`."""
    return [
        {"id": "worker", "ok": worker_imports_ok(), "required": True,
         "detail": f"worker environment at {venv_dir()}", "fix": "pdf2md setup"},
        {"id": "model", "ok": models_present(), "required": True,
         "detail": f"PaddleOCR-VL-1.6 model files in {model_dir()}", "fix": "pdf2md setup"},
        {"id": "llama-server", "ok": llama_server() is not None, "required": False,
         "detail": "llama.cpp `llama-server` on PATH (needed unless a server is already running)",
         "fix": "install llama.cpp (https://github.com/ggml-org/llama.cpp), or set LLAMA_SERVER"},
        {"id": "server", "ok": server_reachable(server), "required": False,
         "detail": f"model server at {server}",
         "fix": "run `pdf2md serve` in another terminal, or pass --serve-command 'pdf2md serve'"},
        {"id": "gpu", "ok": gpu_name() is not None, "required": False,
         "detail": gpu_name() or "no NVIDIA GPU detected by nvidia-smi (other GPUs may work through llama.cpp)",
         "fix": ""},
    ]


def require_ready() -> None:
    bad = [c for c in status_required() if not c["ok"]]
    if bad:
        sys.exit("error: " + "; ".join(f"missing {c['detail']}" for c in bad) + f". {SETUP_HINT}")


def status_required() -> list[dict]:
    return [
        {"id": "worker", "ok": worker_imports_ok(), "detail": f"worker environment ({venv_dir()})"},
        {"id": "model", "ok": models_present(), "detail": f"model files ({model_dir()})"},
    ]


def _run(cmd: list[str]) -> None:
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def _download(url: str, dest: Path) -> None:
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
    with urllib.request.urlopen(req, timeout=60) as r:
        if have and r.status != 206:
            have = 0
        with open(part, "ab" if have else "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
    part.replace(dest)


def setup() -> None:
    venv = venv_dir()
    if not worker_imports_ok():
        venv.parent.mkdir(parents=True, exist_ok=True)
        uv = shutil.which("uv")
        if uv:
            _run([uv, "venv", "--python", WORKER_PYTHON, str(venv)])
            _run([uv, "pip", "install", "--python", str(worker_python()), *WORKER_DEPS])
        else:
            if not (3, 10) <= sys.version_info[:2] < (3, 14):
                sys.exit("error: needs Python 3.10-3.13 (or install `uv`, which can fetch Python 3.12 itself).")
            _run([sys.executable, "-m", "venv", str(venv)])
            _run([str(worker_python()), "-m", "pip", "install", *WORKER_DEPS])
    md = model_dir()
    md.mkdir(parents=True, exist_ok=True)
    for f in MODEL_FILES:
        if not (md / f).is_file():
            print(f"downloading {f} from huggingface.co/{MODEL_REPO} (about 1 GB in total) ...", flush=True)
            try:
                _download(f"https://huggingface.co/{MODEL_REPO}/resolve/main/{f}", md / f)
            except (urllib.error.URLError, OSError) as e:
                sys.exit(f"error: could not download {f}: {e}. Run `pdf2md setup` again to resume.")
    print("setup finished.")
    if not llama_server():
        print("note: `llama-server` (llama.cpp) is not on PATH; install it to serve the model: "
              "https://github.com/ggml-org/llama.cpp")


def serve() -> int:
    """Run llama-server with the PaddleOCR-VL model in the foreground (Ctrl-C stops it)."""
    exe = llama_server()
    if not exe:
        sys.exit("error: llama-server not found. Install llama.cpp (https://github.com/ggml-org/llama.cpp) "
                 "and put llama-server on PATH, or set LLAMA_SERVER.")
    if not models_present():
        sys.exit(f"error: model files missing in {model_dir()}. {SETUP_HINT}")
    env = os.environ
    cmd = [exe, "-m", str(model_dir() / MODEL_FILES[0]), "--mmproj", str(model_dir() / MODEL_FILES[1]),
           "--host", "127.0.0.1", "--port", env.get("PORT", "8111"), "--temp", "0", "-ngl", env.get("NGL", "99"),
           "-c", env.get("CTX", "16384"), "-np", env.get("SLOTS", "3"), "--alias", "paddleocr-vl"]
    os.execv(exe, cmd)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pdf2md " + argv[0])
    if argv[0] == "serve":
        ap.parse_args(argv[1:])
        return serve()
    if argv[0] == "check":
        ap.add_argument("--server", default=os.environ.get("PDF2MD_PADDLE_SERVER", DEFAULT_SERVER))
        ap.add_argument("--json", action="store_true", help="machine-readable output (used by the Obsidian plugin)")
        a = ap.parse_args(argv[1:])
        checks = status(a.server)
        ready = all(c["ok"] for c in checks if c["required"])
        if a.json:
            print(json.dumps({"ready": ready, "checks": checks}))
        else:
            for c in checks:
                mark = "ok     " if c["ok"] else ("MISSING" if c["required"] else "not set")
                print(f"{mark}  {c['detail']}" + ("" if c["ok"] or not c["fix"] else f"\n         -> {c['fix']}"))
            print("ready to convert" if ready else f"not ready. {SETUP_HINT}")
        return 0 if ready else 1
    ap.parse_args(argv[1:])
    setup()
    return 0
