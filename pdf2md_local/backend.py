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
# Pinned Hugging Face revision (commit sha) and per-file (size in bytes, sha256), from the HF API (lfs.oid).
MODEL_REVISION = "511b09642bb324401f15f97cc23bc67e8f0a291d"
MODEL_PINS = {
    "PaddleOCR-VL-1.6-GGUF.gguf": (935769056, "f3ae46ec885050acf4b3d31944431e1fd90d50664fb09126af4a3c050ba14ee8"),
    "PaddleOCR-VL-1.6-GGUF-mmproj.gguf": (881770560, "204d757d7610d9b3faab10d506d69e5b244e32bf765e2bab2d0167e65e0a058a"),
}
LLAMA_CPP_TAG = "b12606"        # the tested llama.cpp build; override with env PDF2MD_LLAMA_TAG
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


def _model_ok(f: str) -> bool:
    """Size check only (no hashing of multi-GB files); the sha256 is verified right after a download."""
    p = model_dir() / f
    return p.is_file() and p.stat().st_size == MODEL_PINS[f][0]


def models_present() -> bool:
    return all(_model_ok(f) for f in MODEL_FILES)


def llama_dir() -> Path:
    return data_dir() / "llama.cpp"


def _installed_llama() -> str | None:
    name = "llama-server.exe" if os.name == "nt" else "llama-server"
    found = sorted(llama_dir().rglob(name)) if llama_dir().is_dir() else []
    return str(found[-1]) if found else None


def llama_server() -> str | None:
    """LLAMA_SERVER, then PATH, then the build that `pdf2md setup` downloaded."""
    cmd = os.environ.get("LLAMA_SERVER", "llama-server")
    return shutil.which(cmd) or (cmd if Path(cmd).is_file() else None) or _installed_llama()


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
         "fix": "pdf2md setup   (downloads a prebuilt llama.cpp), or install it yourself / set LLAMA_SERVER"},
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


def _driver_cuda() -> tuple[int, int] | None:
    """The CUDA version the NVIDIA driver supports, from `nvidia-smi`, or None without an NVIDIA GPU."""
    import re
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    r = subprocess.run([exe], capture_output=True, text=True)
    m = re.search(r"CUDA (?:UMD )?Version:\s*(\d+)\.(\d+)", r.stdout)
    return (int(m[1]), int(m[2])) if m else None


def pick_llama_assets(names: list[str], system: str, machine: str, cuda: tuple[int, int] | None,
                      backend_choice: str = "auto", has_vulkan: bool = False) -> list[str] | None:
    """Choose the llama.cpp release archive(s) (binary, plus the CUDA runtime if needed) from a release's file names."""
    import re
    arm = machine in ("arm64", "aarch64")
    x64 = machine in ("x86_64", "amd64", "AMD64")
    if system == "Darwin":
        want = "macos-arm64" if arm else "macos-x64"
        hit = [n for n in names if re.fullmatch(rf"llama-b\d+-bin-{want}\.tar\.gz", n)]
        return hit or None
    if system not in ("Linux", "Windows") or not x64:
        return None
    ext = r"tar\.gz" if system == "Linux" else "zip"
    osname = "ubuntu" if system == "Linux" else "win"
    want = backend_choice
    if want == "auto":
        want = "cuda" if cuda else ("vulkan" if has_vulkan else "cpu")
    if want == "cuda":
        if not cuda:
            return None
        cands = []
        for n in names:
            m = re.fullmatch(rf"llama-b\d+-bin-{osname}-cuda-(\d+)\.(\d+)-x64\.{ext}", n)
            if m and (int(m[1]), int(m[2])) <= cuda:
                cands.append(((int(m[1]), int(m[2])), n))
        if not cands:
            return None
        ver, binary = max(cands)
        runtime = [n for n in names if re.fullmatch(rf"cudart-llama-(?:b\d+-)?bin-{osname}-cuda-{ver[0]}\.{ver[1]}-x64\.{ext}", n)]
        return [binary] + runtime
    suffix = {"vulkan": "vulkan-x64", "cpu": "x64" if system == "Linux" else "cpu-x64"}.get(want)
    hit = [n for n in names if suffix and re.fullmatch(rf"llama-b\d+-bin-{osname}-{suffix}\.{ext}", n)]
    return hit or None


def _has_tar_filter() -> bool:
    import tarfile
    return hasattr(tarfile, "data_filter")


def _tar_member_unsafe(m, dest: Path) -> bool:
    """Manual check for Pythons without tarfile extraction filters."""
    def escapes(p: Path) -> bool:
        return not p.resolve().is_relative_to(dest)
    if m.isdev() or escapes(dest / m.name):
        return True
    if m.issym():
        return os.path.isabs(m.linkname) or escapes((dest / m.name).parent / m.linkname)
    if m.islnk():
        return os.path.isabs(m.linkname) or escapes(dest / m.linkname)
    return False


def _safe_extract(archive: Path, dest: Path) -> None:
    import tarfile
    import zipfile
    dest = dest.resolve()
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for m in z.namelist():
                if not (dest / m).resolve().is_relative_to(dest):
                    sys.exit(f"error: unsafe path in {archive.name}")
            z.extractall(dest)
        return
    with tarfile.open(archive) as t:
        if _has_tar_filter():
            try:
                t.extractall(dest, filter="data")
            except tarfile.FilterError:
                sys.exit(f"error: unsafe path in {archive.name}")
        else:
            if any(_tar_member_unsafe(m, dest) for m in t.getmembers()):
                sys.exit(f"error: unsafe path in {archive.name}")
            t.extractall(dest)   # members were checked above: nothing escapes dest


def _verify_file(file: Path, size: int | None, sha256: str | None, what: str) -> None:
    """Check size and sha256 (hashed in 1 MB chunks); on mismatch delete the file and exit."""
    import hashlib
    bad = None
    if size is not None and file.stat().st_size != size:
        bad = f"size {file.stat().st_size} instead of {size}"
    elif sha256:
        h = hashlib.sha256()
        with open(file, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != sha256.lower():
            bad = "sha256 differs"
    if bad:
        file.unlink()
        sys.exit(f"error: checksum mismatch for {what} ({bad}); the file was deleted. Run `pdf2md setup` again.")


def install_llama(choice: str = "auto", allow_unverified: bool = False) -> None:
    """Download the pinned prebuilt llama.cpp (llama-server) into the data folder. Run only by `pdf2md setup`."""
    import platform
    tag = os.environ.get("PDF2MD_LLAMA_TAG") or LLAMA_CPP_TAG
    api = f"https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{tag}"
    try:
        with urllib.request.urlopen(urllib.request.Request(api, headers={"Accept": "application/vnd.github+json"}), timeout=60) as r:
            rel = json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError) as e:
        sys.exit(f"error: could not get llama.cpp release {tag} ({e}). Install llama.cpp yourself and put llama-server on PATH.")
    vulkan = bool(shutil.which("vulkaninfo")) or any(Path(p).exists() for p in ("/usr/lib/libvulkan.so.1", "/usr/lib64/libvulkan.so.1", "/usr/lib/x86_64-linux-gnu/libvulkan.so.1"))
    assets = {a["name"]: a for a in rel.get("assets", [])}
    picked = pick_llama_assets(list(assets), platform.system(), platform.machine(), _driver_cuda(), choice, vulkan)
    if not picked:
        sys.exit(f"error: no prebuilt llama.cpp {tag} matches this system. Build it from source (docs/install.md) "
                 "and put llama-server on PATH.")
    missing = [n for n in picked if not (assets[n].get("digest") or "").startswith("sha256:")]
    if missing and not allow_unverified:
        sys.exit(f"error: GitHub gives no sha256 digest for {', '.join(missing)}, so the download cannot be verified. "
                 "Run `pdf2md setup --allow-unverified` to install it anyway.")
    dest = llama_dir() / tag
    dest.mkdir(parents=True, exist_ok=True)
    total = sum(assets[n]["size"] for n in picked) // 1_000_000
    print(f"installing llama.cpp {tag}: {', '.join(picked)} (about {total} MB) into {dest}", flush=True)
    for name in picked:
        a = assets[name]
        file = dest / name
        _download(a["browser_download_url"], file, a.get("size"))
        digest = (a.get("digest") or "").removeprefix("sha256:")
        if digest:
            _verify_file(file, None, digest, name)
        _safe_extract(file, dest)
        file.unlink()
    exe = _installed_llama()
    if not exe:
        sys.exit("error: the downloaded archive did not contain llama-server.")
    os.chmod(exe, 0o755)
    print(f"llama-server ready: {exe}")


def _run(cmd: list[str]) -> None:
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def _validator(url: str) -> str | None:
    """ETag (or Last-Modified) from a HEAD request, for If-Range; None when unavailable."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60) as r:
            return r.headers.get("ETag") or r.headers.get("Last-Modified")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _download(url: str, dest: Path, expected_size: int | None = None) -> None:
    """Resumable download to dest (via dest.part). Never leaves a truncated file under the final name."""
    import http.client
    import re
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    try:
        if expected_size is not None and have > expected_size:
            part.unlink()
            have = 0
        total = expected_size
        if have and have == expected_size:
            pass                                    # already complete
        else:
            headers = {}
            if have:
                validator = _validator(url)
                if validator:
                    headers = {"Range": f"bytes={have}-", "If-Range": validator}
                else:
                    have = 0                        # cannot resume safely
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                    status = getattr(r, "status", 200)
                    if have and status != 206:
                        have = 0                    # server ignored the range: start again
                    length = r.headers.get("Content-Length")
                    cr = re.fullmatch(r"bytes\s+\d+-\d+/(\d+)", r.headers.get("Content-Range") or "")
                    if total is None:
                        if cr:
                            total = int(cr[1])
                        elif length is not None:
                            total = have + int(length)
                    with open(part, "ab" if have else "wb") as f:
                        shutil.copyfileobj(r, f, 1 << 20)
            except urllib.error.HTTPError as e:
                if e.code != 416 or not have:
                    raise
                m = re.fullmatch(r"bytes\s+\*/(\d+)", e.headers.get("Content-Range") or "") if e.headers else None
                if total is None and m:
                    total = int(m[1])
                if total is not None and have != total:
                    part.unlink()
                    sys.exit(f"error: partial download of {dest.name} does not match the server; "
                             "it was deleted. Run `pdf2md setup` again.")
        size = part.stat().st_size
        if total is not None and size < total:
            sys.exit(f"error: download of {dest.name} was cut off ({size} of {total} bytes). "
                     "Run `pdf2md setup` again to resume.")
        if total is not None and size > total:
            part.unlink()
            sys.exit(f"error: download of {dest.name} is larger than expected; it was deleted. Run `pdf2md setup` again.")
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        sys.exit(f"error: could not download {dest.name}: {e}. Run `pdf2md setup` again to resume.")
    part.replace(dest)


def setup(llama: str = "auto", llama_only: bool = False, allow_unverified: bool = False) -> None:
    venv = venv_dir()
    if llama != "skip" and not llama_server():
        install_llama(llama, allow_unverified)
    if llama_only:
        return
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
        if not _model_ok(f):
            size, sha = MODEL_PINS[f]
            print(f"downloading {f} from huggingface.co/{MODEL_REPO}@{MODEL_REVISION[:8]} (about 1 GB in total) ...", flush=True)
            _download(f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{f}", md / f, size)
            print(f"verifying {f} ...", flush=True)
            _verify_file(md / f, size, sha, f)
    print("setup finished.")
    if not llama_server():
        print("note: no llama-server yet (skipped); install llama.cpp, or run `pdf2md setup` again to download one.")


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


def selftest(server: str = DEFAULT_SERVER) -> int:
    """Convert the bundled sample PDF and check the result, so an installation can be verified in a minute."""
    import tempfile
    sample = Path(__file__).with_name("data") / "sample.pdf"
    if not sample.is_file():
        sys.exit("error: the bundled sample.pdf is missing; reinstall pdf2md-local.")
    require_ready()
    serve = f"{sys.executable} -m pdf2md_local serve"
    with tempfile.TemporaryDirectory(prefix="pdf2md-selftest-") as tmp:
        cmd = [sys.executable, "-m", "pdf2md_local", str(sample), "--split-chapters", "--flavor", "plain",
               "--no-cache", "--no-backup", "-o", tmp, "--paddle-server", server, "--serve-command", serve]
        print("converting the bundled 2-page sample (about 10-30 seconds) ...", flush=True)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print((r.stdout + r.stderr)[-1500:])
            print("FAILED: the conversion did not finish.")
            return 1
        folder = Path(tmp) / "sample"
        notes = {p.name: p.read_text(encoding="utf-8") for p in folder.glob("*.md")}
        text = "\n".join(notes.values())
        checks = [
            ("two chapter notes and an index", len(notes) == 3 and "00 Index.md" in notes),
            ("chapter titles found", "Calculus essentials" in text and "Linear algebra" in text),
            ("display equations as $$ blocks", text.count("$$") >= 4),
            ("a Gaussian-integral equation", "\\sqrt{\\pi}" in text.replace(" ", "")),
            ("a Markdown table", "| Function |" in text and "| --- |" in text),
            ("an image saved for the figure", any((folder / "assets").glob("*.*"))),
            ("standard links (no wikilinks)", "[[" not in text),
        ]
    for name, ok in checks:
        print(f"{'ok     ' if ok else 'FAILED '} {name}")
    good = all(ok for _, ok in checks)
    print("self-test passed" if good else "self-test finished with differences; OCR output can vary, "
          "compare with examples/expected/ in the repository before reporting a problem")
    return 0 if good else 1


DEFAULT_SEC_PER_PAGE = 3.4      # measured on a 16 GB GPU over two whole books; replaced by your own runs
STARTUP_SECONDS = 15


def rate_file() -> Path:
    return data_dir() / "rate.json"


def read_rate() -> tuple[float, bool]:
    """Seconds per page: your own smoothed measurement if there is one, else the default."""
    try:
        v = float(json.loads(rate_file().read_text())["sec_per_page"])
        if 0.05 < v < 600:
            return v, True
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return DEFAULT_SEC_PER_PAGE, False


def record_rate(pages: int, seconds: float) -> None:
    """Remember how fast this machine converts (only for runs long enough to be meaningful)."""
    if pages < 20 or seconds <= 0:
        return
    new = seconds / pages
    old, measured = read_rate()
    try:
        rate_file().parent.mkdir(parents=True, exist_ok=True)
        rate_file().write_text(json.dumps({"sec_per_page": round(0.5 * old + 0.5 * new if measured else new, 3)}))
    except OSError:
        pass


def info(argv: list[str]) -> int:
    """Pages and an estimated time for PDFs, before converting them."""
    from pdf2md_local import convert
    ap = argparse.ArgumentParser(prog="pdf2md info")
    ap.add_argument("pdf", type=Path, nargs="+")
    ap.add_argument("--pages", help="only these pages, e.g. 10-40 (same as pdf2md --pages)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rate, measured = read_rate()
    files = []
    for pdf in a.pdf:
        total = convert.page_count(pdf) if pdf.is_file() else None
        n = None
        if total:
            first, last = convert.parse_pages(a.pages, total) if len(a.pdf) == 1 else (1, total)
            n = (last or total) - first + 1
        files.append({"name": pdf.name, "pages": n, "seconds": None if n is None else round(n * rate + STARTUP_SECONDS)})
    known = [f for f in files if f["pages"]]
    out = {"files": files, "pages": sum(f["pages"] for f in known), "seconds": sum(f["seconds"] for f in known),
           "unknown": len(files) - len(known), "sec_per_page": rate, "measured": measured}
    if a.json:
        print(json.dumps(out))
    else:
        for f in files:
            print(f"{f['name']}: {f['pages'] or '?'} pages, about {round((f['seconds'] or 0) / 60, 1)} min")
        print(f"total: {out['pages']} pages, about {round(out['seconds'] / 60, 1)} min "
              f"({'your measured speed' if measured else 'default speed'}: {rate:.1f} s/page)")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="pdf2md " + argv[0], epilog=(
        f"llama.cpp is pinned to build {LLAMA_CPP_TAG}; set env PDF2MD_LLAMA_TAG to use another release tag.")
        if argv[0] == "setup" else None)
    if argv[0] == "serve":
        ap.parse_args(argv[1:])
        return serve()
    if argv[0] == "info":
        return info(argv[1:])
    if argv[0] == "selftest":
        ap.add_argument("--server", default=os.environ.get("PDF2MD_PADDLE_SERVER", DEFAULT_SERVER))
        return selftest(ap.parse_args(argv[1:]).server)
    if argv[0] == "check":
        ap.add_argument("--server", default=os.environ.get("PDF2MD_PADDLE_SERVER", DEFAULT_SERVER))
        ap.add_argument("--json", action="store_true", help="machine-readable output (used by the Obsidian plugin)")
        a = ap.parse_args(argv[1:])
        checks = status(a.server)
        ready = all(c["ok"] for c in checks if c["required"])
        if a.json:
            from pdf2md_local import __version__
            print(json.dumps({"ready": ready, "version": __version__, "checks": checks}))
        else:
            for c in checks:
                mark = "ok     " if c["ok"] else ("MISSING" if c["required"] else "not set")
                print(f"{mark}  {c['detail']}" + ("" if c["ok"] or not c["fix"] else f"\n         -> {c['fix']}"))
            print("ready to convert" if ready else f"not ready. {SETUP_HINT}")
        return 0 if ready else 1
    ap.add_argument("--llama", choices=("auto", "cuda", "vulkan", "cpu", "skip"), default="auto",
                    help="which prebuilt llama.cpp to download if llama-server is not found (default: auto-detect; skip = never)")
    ap.add_argument("--llama-only", action="store_true", help="only install llama-server")
    ap.add_argument("--allow-unverified", action="store_true",
                    help="install llama.cpp even if GitHub provides no sha256 digest for the download")
    a = ap.parse_args(argv[1:])
    setup(a.llama, a.llama_only, a.allow_unverified)
    return 0
