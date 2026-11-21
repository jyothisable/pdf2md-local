"""Running the conversion: server guard, worker process, checkpoint cache, page slicing.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from pdf2md_local.notes import clean_paddle_markdown, failed_marker
from pdf2md_local.outline import link_target

# Output caps (tokens): a runaway generation on a garbled crop must not tie up a slot for minutes.


def api_base(host: str) -> str:
    """Base URL of an OpenAI-compatible server. A bare host:port gets /v1 (llama.cpp, LM Studio, vLLM)."""
    host = host.strip().rstrip("/")
    return host + "/v1" if urllib.parse.urlparse(host).path in ("", "/") else host


def openai_chat(host: str, model: str, system: str | None, user: str, timeout: int,
                images: list[bytes] | None = None, max_out: int | None = None,
                api_key: str | None = None, require_complete: bool = False) -> str:
    """One chat completion from any OpenAI-compatible server (llama.cpp's llama-server, LM Studio, vLLM, ...)."""
    text = {"type": "text", "text": user}
    imgs = [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(i).decode()}}
            for i in images or []]
    content = [text, *imgs]
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": content}]
    body = {"model": model, "stream": False, "temperature": 0, "messages": messages}
    if max_out:
        body["max_tokens"] = max_out
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(f"{api_base(host)}/chat/completions", json.dumps(body).encode(), headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        choice = json.load(r)["choices"][0]
        if require_complete and choice.get('finish_reason') != 'stop':
            raise ValueError('incomplete model response')
        return choice["message"]["content"]


class Cache:
    """Checkpoint store so an interrupted run resumes instead of starting over."""

    def __init__(self, root: Path):
        self.root = root
        self.img = root / "img"
        self.img.mkdir(parents=True, exist_ok=True)

    def get(self, name: str):
        f = self.root / f"{name}.json"
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def put(self, name: str, obj) -> None:
        tmp = self.root / f"{name}.json.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(obj), encoding="utf-8")
        tmp.replace(self.root / f"{name}.json")  # atomic: a crash never leaves a half-written entry


def run_key(pdf: Path, *parts) -> str:
    st = pdf.stat()
    src = b"".join(f.read_bytes() for f in sorted(Path(__file__).parent.glob("*.py")))  # any code change invalidates old checkpoints
    worker = _convert().WORKER  # looked up on convert so tests can patch it
    if worker.is_file():
        src += worker.read_bytes()
    raw = "|".join(map(str, (pdf, st.st_size, st.st_mtime_ns, hashlib.sha1(src).hexdigest(), *parts)))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def new_stats() -> dict:
    return {"eq_total": 0, "eq_tidied": 0, "eq_repaired": 0, "eq_unresolved": 0, "eq_as_text": 0, "eq_pictured": 0, "eq_flagged": 0,
            "figs_described": 0, "inline_total": 0, "inline_rewritten": 0, "inline_retried_ok": 0,
            "prose_restored": 0, "composite_figures": 0, "inline_source_restored": 0, "inline_source_images": 0,
            "running_headers": 0}


def merge_stats(a: dict, b: dict) -> dict:
    out: dict = {}
    for k in {**a, **b}:
        x, y = a.get(k, 0), b.get(k, 0)
        if isinstance(x, list) or isinstance(y, list):      # e.g. pages with formula pictures
            out[k] = sorted(set(x or []) | set(y or []))
        else:
            out[k] = x + y
    return out


def convert_resilient(convert_fn: Callable[[tuple[int, int]], tuple[str, dict]],
                      rng: tuple[int, int], failed: list[int],
                      cleanup: Callable[[], None] | None = None) -> tuple[str, dict]:
    """Convert a slice; on failure retry in halves, retry a single page once, then mark it."""
    try:
        return convert_fn(rng)
    except Exception:  # noqa: BLE001 - a bad page must not kill a multi-hour run
        lo, hi = rng
        if hi >= 10**8:
            raise  # unknown page count: cannot split
        if cleanup:
            cleanup()
        if hi > lo:
            mid = (lo + hi) // 2
            md1, s1 = convert_resilient(convert_fn, (lo, mid), failed, cleanup)
            md2, s2 = convert_resilient(convert_fn, (mid + 1, hi), failed, cleanup)
            return md1.rstrip("\n") + "\n\n" + md2, merge_stats(s1, s2)
        try:  # a lone page: one more try, since GPU memory errors are often transient
            return convert_fn(rng)
        except Exception as e2:  # noqa: BLE001
            failed.append(lo)
            print(f"warning: page {lo} could not be converted ({e2.__class__.__name__}: {e2})", file=sys.stderr, flush=True)
            return failed_marker(lo) + "\n", new_stats()


# ----------------------------------------------------------------------------
# Inline math: paragraphs whose maths survived only as flattened Unicode text
# ----------------------------------------------------------------------------


def page_ranges(total: int | None, batch: int, first: int = 1) -> list[tuple[int, int]]:
    """1-based inclusive page slices from `first` to `total`; one slice when the size is unknown or small."""
    if not total or batch <= 0 or total - first + 1 <= batch:
        return [(first, total or 10**9)]
    return [(lo, min(lo + batch - 1, total)) for lo in range(first, total + 1, batch)]


def parse_pages(spec: str | None, total: int | None) -> tuple[int, int | None]:
    """'A-B' or 'A' -> (first, last), clamped to the document; None -> the whole file."""
    if not spec:
        return 1, total
    m_ = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", spec)
    if not m_:
        sys.exit(f"error: --pages must look like 5 or 10-40, got '{spec}'")
    lo = int(m_.group(1))
    hi = int(m_.group(2) or lo)
    if total:
        hi = min(hi, total)
    if lo < 1 or hi < lo:
        sys.exit(f"error: page range {spec} is outside the document ({total or '?'} pages)")
    return lo, hi


def page_count(pdf: Path) -> int | None:
    """Number of pages: pypdf (a dependency of this package) first, pypdfium2 if that is all that is installed."""
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(pdf)).pages)
    except Exception:  # noqa: BLE001
        pass
    try:
        import pypdfium2
        d = pypdfium2.PdfDocument(str(pdf))
        try:
            return len(d)
        finally:
            d.close()
    except Exception:  # noqa: BLE001
        return None


WORKER = Path(__file__).with_name("paddle_worker.py")
PACKAGE_PARENT = Path(__file__).resolve().parent.parent


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class CacheLock:
    """One conversion per checkpoint folder: an exclusive lock file holding our pid; a stale one is taken over."""

    def __init__(self, root: Path):
        self.path = Path(root) / "run.lock"
        self.held = False

    def acquire(self) -> None:
        for _ in range(5):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    pid = int(self.path.read_text().strip() or 0)
                except (OSError, ValueError):
                    pid = 0
                if pid and pid != os.getpid() and _pid_alive(pid):
                    sys.exit(f"error: another conversion of this PDF is running (process {pid}); wait for it to finish, "
                             f"or stop it and run again.")
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            self.held = True
            return
        sys.exit("error: could not lock the checkpoint folder for this PDF (another conversion is starting?)")

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        try:
            if self.path.read_text().strip() == str(os.getpid()):
                self.path.unlink()
        except OSError:
            pass


class ServerGuard:
    """Make sure an OpenAI-compatible server (llama-server) answers; start it with `command` if it does not,
    and stop it again afterwards if this run started it. A server that was already running is never touched."""

    def __init__(self, url: str, command: str | None, wait: int = 180):
        from pdf2md_local import backend
        self.url, self.command, self.wait = url, command, wait
        self.proc: subprocess.Popen | None = None
        self.log = backend.data_dir() / "logs" / "llama-server.log"

    def alive(self) -> bool:
        try:
            with urllib.request.urlopen(f"{api_base(self.url)}/models", timeout=3):
                return True
        except urllib.error.HTTPError:
            return True        # it answered
        except (urllib.error.URLError, OSError):
            return False

    def __enter__(self) -> "ServerGuard":
        if self.alive():
            return self
        if not self.command:
            sys.exit(f"error: no PaddleOCR-VL server at {api_base(self.url)}. Start one, for example:\n"
                     f"  llama-server -m PaddleOCR-VL-1.6-GGUF.gguf --mmproj PaddleOCR-VL-1.6-GGUF-mmproj.gguf "
                     f"--port 8111 --temp 0 -ngl 99\nor run `pdf2md serve`, or pass --serve-command.")
        print(f"      starting the model server: {self.command}", flush=True)
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log, "w") as f:
            self.proc = subprocess.Popen(shlex.split(self.command), stdout=f, stderr=subprocess.STDOUT,
                                         start_new_session=True)
        deadline = time.time() + self.wait
        while time.time() < deadline:
            if self.proc.poll() is not None:
                sys.exit(f"error: the model server exited (code {self.proc.returncode}); see {self.log}")
            if self.alive():
                return self
            time.sleep(1)
        self.__exit__()
        sys.exit(f"error: the model server did not become ready within {self.wait}s; see {self.log}")

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, 15)
                self.proc.wait(timeout=15)
            except Exception:  # noqa: BLE001
                try:
                    os.killpg(self.proc.pid, 9)
                except Exception:  # noqa: BLE001
                    self.proc.kill()
                try:
                    self.proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
        self.proc = None


def _convert():
    """The convert module, looked up at call time so tests can patch worker_command there."""
    from pdf2md_local import convert
    return convert


def worker_command(pdf: Path, rng: tuple[int, int], server: str, out: Path, scale: float = 2.5) -> list[str]:
    from pdf2md_local import backend
    py, script = str(backend.worker_python()), str(WORKER)
    if os.environ.get("PDF2MD_TEST_WORKER_SCRIPT"):      # for tests only: run this script with this Python instead
        py, script = sys.executable, os.environ["PDF2MD_TEST_WORKER_SCRIPT"]
    return [py, script, "--pdf", str(pdf), "--first", str(rng[0]),
            "--last", str(rng[1]), "--server", server, "--out", str(out), "--scale", str(scale)]


def _stop_process(proc: subprocess.Popen) -> None:
    """Terminate, give it 5 s, then kill."""
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        except OSError:
            pass


def paddle_slice(pdf: Path, page_range: tuple[int, int], assets_dir: Path, server: str, page_links: bool,
                 on_page: Callable[[int, int], None] | None = None, scale: float = 2.5) -> tuple[str, dict]:
    """Convert one page slice with the PaddleOCR-VL worker; on_page(n, total) is called as each page finishes.
    Pages the worker reports as ERROR, or that are missing from its output (when the range is known), are written
    as `<!-- pdf2md:failed page=N -->`. stats["formula_image_pages"] lists pages with formulas kept as pictures."""
    stats = new_stats()
    with tempfile.TemporaryDirectory() as tmp:
        # The worker venv's own packages (numpy, pillow, paddle) must win: the package parent is only appended.
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PDF2MD_PACKAGE_PARENT": str(PACKAGE_PARENT)}
        proc = subprocess.Popen(_convert().worker_command(pdf, page_range, server, Path(tmp), scale), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=env)
        bad: set[int] = set()
        tail: collections.deque = collections.deque(maxlen=40)
        try:
            for line in proc.stdout:
                tail.append(line.rstrip("\n"))
                m = re.match(r"PAGE (\d+)/(\d+)", line)
                if m and on_page:
                    on_page(int(m.group(1)), int(m.group(2)))
                m = re.match(r"ERROR page (\d+)", line)
                if m:
                    bad.add(int(m.group(1)))
            rc = proc.wait()
        finally:
            _stop_process(proc)
            proc.stdout.close()
        dirs = sorted(d for d in Path(tmp).glob("page_*") if int(d.name[5:]) not in bad)
        if rc != 0 and not dirs:
            raise RuntimeError(f"the PaddleOCR-VL worker failed (exit {rc}); last output:\n" + "\n".join(tail))
        if rc != 0:
            print(f"warning: the PaddleOCR-VL worker exited with code {rc}; last output:\n" + "\n".join(tail),
                  file=sys.stderr, flush=True)
        lo, hi = page_range
        if hi < 10**8:
            bad |= set(range(lo, hi + 1)) - {int(d.name[5:]) for d in dirs}
        pieces: dict[int, str] = {}
        formula_pages: set[int] = set()
        for d in dirs:
            page_no = int(d.name[5:])
            texts = [f.read_text(encoding="utf-8") for f in sorted(d.glob("*.md"))]
            if texts:
                link = link_target(pdf.name, page_no) if page_links else None
                pieces[page_no] = (f"<!-- pdf2md:page={page_no} -->\n\n" +
                                   clean_paddle_markdown("\n\n".join(texts), page_no, d, assets_dir, link))
            safety_file = d / 'safety.json'
            if safety_file.is_file():
                for report in json.loads(safety_file.read_text()):
                    stats['eq_repaired'] += len(report['formula_retries'])
                    stats['eq_pictured'] += len(report['formula_images'])
                    if report['formula_images']:
                        formula_pages.add(page_no)
                    stats['prose_restored'] = stats.get('prose_restored',0) + len(report['prose_restored'])
                    stats['composite_figures'] = stats.get('composite_figures',0) + len(report['composite_figures'])
                    stats['inline_source_restored'] += len(report.get('inline_restored',[]))
                    stats['inline_source_images'] += len(report.get('inline_images',[]))
                    stats['running_headers'] += len(report.get('running_headers',[]))
        for n in bad:
            pieces[n] = failed_marker(n)
        parts = [pieces[n] for n in sorted(pieces)]
        stats["eq_total"] = sum(len(re.findall(r"\$\$.+?\$\$", t, re.S)) for t in parts)
        stats['eq_total'] += stats['eq_pictured']
        stats["formula_image_pages"] = sorted(formula_pages)
        return "\n\n".join(parts), stats


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------
