# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["docling==2.150.1", "pypdf>=6,<7"]
# ///
"""Convert PDFs to Obsidian-flavoured Markdown with a local model.

Two engines (--engine):

  paddleocr-vl  PaddleOCR-VL-1.6: Paddle's layout model reads each page, a 0.9B vision-language model served by
                llama.cpp's llama-server reads each block. Best on maths in the project's tests, small (about 3 GB
                of VRAM) and no Ollama; weaker on tables and scans. Needs a running server (--paddle-server)
                or --serve-command to start one.
  docling       Docling parses the layout; a vision model (Ollama, or any OpenAI-compatible server) re-reads
                equations and rewrites maths paragraphs as LaTeX.

Both feed the same finishing steps:
  - maths is normalised: inline $...$, display $$ on their own lines; the LaTeX is checked for rendering errors
  - headings are tidied, optional links to the PDF page are added under them, books can be split into chapters
  - the note is written to a NEW file (never overwrites), figures as ![[embeds]]
  - long runs checkpoint per slice and resume; one bad page is retried, then marked, never fatal

With the docling engine a text model can also clean prose (--clean): maths, tables, code and images are swapped
for placeholders first so the model cannot alter them.

Run:  pdf2md paper.pdf [more.pdf ...]            (see pdf2md --help)
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import difflib
import hashlib
import html.parser
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")  # less fragmentation next to Ollama

DEFAULT_MODEL = "gemma4:12b"
DEFAULT_HOST = "http://localhost:11434"
CHUNK_CHARS = 5000
NUM_CTX = 8192           # same for every call, or Ollama reloads the model
DEFAULT_JOBS = 3         # parallel Ollama requests; keep <= OLLAMA_NUM_PARALLEL on the server
PAGES_PER_BATCH = 40     # Docling converts big PDFs in slices of this many pages
CLEAN_MODES = ("auto", "all", "none")
ENGINES = ("docling", "paddleocr-vl")
PADDLE_PAGES_PER_BATCH = 120   # the Paddle worker loads its models once per slice, so use bigger slices
DEFAULT_PADDLE_SERVER = "http://127.0.0.1:8111/v1"
CACHE_ROOT = Path.home() / ".cache" / "pdf2md"
MIN_SPLIT_CHARS = 1200
NOT_DECODED = "formula-not-decoded"

# ----------------------------------------------------------------------------
# LaTeX tidying and validation
# ----------------------------------------------------------------------------
TEXT_CMD = re.compile(r"\\(text|mathrm|operatorname|mathbf|mathit|mbox)\s*\{([^{}]*)\}")
TRAILING_TAG = re.compile(r"(?:&\s*)+\(\s*([\d.\s]+?)\s*\)\s*$")
SINGLE_LETTERS = re.compile(r"(?<![\\A-Za-z])([A-Za-z]) (?=[A-Za-z](?![A-Za-z]))")


def tidy_tex(seg: str) -> str:
    """Remove OCR spacing from LaTeX outside \\text{...} groups. Idempotent."""
    seg = re.sub(r"\\overbar\b", r"\\overline", seg)
    seg = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", seg)
    seg = re.sub(r"(?<=\d) (?=\d)", "", seg)
    for _ in range(3):  # overlapping runs: "a b c" needs more than one pass
        seg = SINGLE_LETTERS.sub(r"\1", seg)
    seg = re.sub(r"\s*([_^])\s*", r"\1", seg)
    seg = re.sub(r"\{\s+", "{", seg)
    seg = re.sub(r"\s+\}", "}", seg)
    seg = re.sub(r"\s+\{", "{", seg)
    seg = re.sub(r"\(\s+", "(", seg)
    seg = re.sub(r"\s+\)", ")", seg)
    seg = re.sub(r"\s*,\s*", ", ", seg)
    seg = re.sub(r"(?<=[{(^=,])-\s+(?=\d)", "-", seg)  # unary minus before a number
    return re.sub(r"[ \t]{2,}", " ", seg)


def tidy_block(body: str) -> str:
    """Fix Docling formula artefacts: spacing, stray & / equation numbers."""
    body = body.strip()
    body = TRAILING_TAG.sub(lambda m: r" \tag{" + m.group(1).replace(" ", "") + "}", body)
    keep: list[str] = []

    def stash(m: re.Match) -> str:
        keep.append(f"\\{m.group(1)}{{{m.group(2).strip()}}}")
        return f"\x01{len(keep) - 1}\x01"

    body = TEXT_CMD.sub(stash, body)
    body = tidy_tex(body)
    body = re.sub(r"\x01(\d+)\x01", lambda m: keep[int(m.group(1))], body)
    if "\\begin" not in body and ("&" in re.sub(r"\\&", "", body) or "\\\\" in body):
        body = "\\begin{aligned}\n" + body.strip() + "\n\\end{aligned}"
    return body.strip()


LATEX_TOKEN = re.compile(
    r"\\begin\{([A-Za-z*]+)\}|\\end\{([A-Za-z*]+)\}|\\left(?![A-Za-z])|\\right(?![A-Za-z])"
    r"|\\\\|\\[{}$&%_#]|\\[A-Za-z]+|\\.|[{}&$]")


def blank_text_groups(s: str) -> str:
    """Empty the contents of \\text{...} groups (any nesting): prose and $..$ inside them are valid."""
    out, i = [], 0
    for m_ in re.finditer(r"\\(?:text|mbox|textrm|textbf|textit)\s*\{", s):
        if m_.start() < i:
            continue
        depth, j = 1, m_.end()
        while j < len(s) and depth:
            depth += (s[j] == "{") - (s[j] == "}")
            j += 1
        if depth:      # unbalanced: leave it for the brace check to report
            break
        out.append(s[i:m_.end()] + "}")
        i = j
    out.append(s[i:])
    return "".join(out)


def latex_problems(s: str) -> list[str]:
    """Cheap structural check for math that MathJax would refuse to render."""
    if not s.strip():
        return ["empty equation"]
    if NOT_DECODED in s:
        return ["formula not decoded"]
    s = blank_text_groups(s)
    probs: list[str] = []
    depth = left = right = 0
    envs: list[str] = []
    for m in LATEX_TOKEN.finditer(s):
        t = m.group(0)
        if m.group(1):
            envs.append(m.group(1))
        elif m.group(2):
            if not envs or envs.pop() != m.group(2):
                probs.append(f"mismatched \\end{{{m.group(2)}}}")
        elif t == "\\left" or (t.startswith("\\left") and not t[5:6].isalpha()):   # not \leftarrow, \leftrightarrow ...
            left += 1
        elif t == "\\right" or (t.startswith("\\right") and not t[6:7].isalpha()):  # not \rightarrow, \rightleftharpoons ...
            right += 1
        elif t == "{":
            depth += 1
        elif t == "}":
            depth -= 1
            if depth < 0:
                probs.append("unbalanced }")
                depth = 0
        elif t in ("&", "\\\\") and not envs and depth == 0:
            probs.append(f"'{t}' outside an environment")
        elif t == "$":
            probs.append("stray $")
    if depth > 0:
        probs.append("unclosed {")
    if envs:
        probs.append(f"unclosed \\begin{{{envs[-1]}}}")
    if left != right:
        probs.append("\\left/\\right count differs")
    return sorted(set(probs))


# ----------------------------------------------------------------------------
# Math normalisation (document level)
# ----------------------------------------------------------------------------
FENCE_RE = re.compile(r"^(```|~~~).*?^\1[ \t]*$", re.S | re.M)
BRACKET_BLOCK = re.compile(r"\\\[(.+?)\\\]", re.S)
PAREN_INLINE = re.compile(r"\\\((.+?)\\\)", re.S)
DOLLAR_BLOCK = re.compile(r"(?<!\\)\$\$(.+?)(?<!\\)\$\$", re.S)
DOLLAR_INLINE = re.compile(r"(?<![\\$])\$(?!\$)([^\n$]+?)(?<![\\$])\$(?!\$)")


def _block(m: re.Match) -> str:
    body = tidy_block(m.group(1))
    return f"\n\n$$\n{body}\n$$\n\n" if body else ""


def _inline(m: re.Match) -> str:
    body = m.group(1).strip()
    return f"${body}$" if body else ""


def normalize_math(md: str) -> str:
    """Make math Obsidian-style: $inline$ and $$ blocks with $$ on their own lines."""
    parts, last = [], 0
    for f in FENCE_RE.finditer(md):  # leave code fences alone
        parts.append((md[last:f.start()], True))
        parts.append((f.group(0), False))
        last = f.end()
    parts.append((md[last:], True))

    out = []
    for text, convert in parts:
        if convert:
            text = BRACKET_BLOCK.sub(_block, text)
            text = DOLLAR_BLOCK.sub(_block, text)
            blocks: list[str] = []

            def stash(m: re.Match) -> str:
                blocks.append(m.group(0))
                return f"\x00B{len(blocks) - 1}\x00"

            text = re.sub(r"\$\$\n.*?\n\$\$", stash, text, flags=re.S)
            text = PAREN_INLINE.sub(_inline, text)
            text = DOLLAR_INLINE.sub(_inline, text)
            text = re.sub(r"\x00B(\d+)\x00", lambda m: blocks[int(m.group(1))], text)
        out.append(text)
    return re.sub(r"\n{3,}", "\n\n", "".join(out))


def validate_document(md: str) -> list[tuple[int, str, str]]:
    """Return (line, problem, snippet) for math likely to fail rendering in Obsidian."""
    issues: list[tuple[int, str, str]] = []
    in_code = in_math = False
    start, buf = 0, []
    for no, line in enumerate(md.split("\n"), 1):
        s = line.strip()
        if s.startswith(("```", "~~~")):
            in_code = not in_code
            continue
        if in_code:
            continue
        if s == "$$":
            if in_math:
                body = "\n".join(buf)
                for p in latex_problems(body):
                    issues.append((start, p, body.replace("\n", " ")[:70]))
                buf = []
            else:
                start = no
            in_math = not in_math
            continue
        if in_math:
            buf.append(line)
            continue
        for m in DOLLAR_INLINE.finditer(line):
            for p in latex_problems(m.group(1)):
                issues.append((no, p, m.group(0)[:70]))
    if in_math:
        issues.append((start, "unclosed $$ block", ""))
    return issues


# ----------------------------------------------------------------------------
# Protect / restore (so the LLM never touches math, tables, code, images)
# ----------------------------------------------------------------------------
def token(i: int) -> str:
    return f"\u27e6P{i}\u27e7"


TOKEN_RE = re.compile(r"\u27e6P(\d+)\u27e7")
FIGURE_CALLOUT = "> [!note] Figure description (auto-generated)"
PROTECT_PATTERNS = [
    re.compile(r"^> \[!note\] Figure description \(auto-generated\)\n(?:>.*\n?)*", re.M),
    re.compile(r"^(```|~~~).*?^\1[ \t]*$", re.S | re.M),      # fenced code
    re.compile(r"\$\$\n.*?\n\$\$", re.S),                     # block math
    re.compile(r"(?:^\|.*\|[ \t]*\n?)+", re.M),               # tables
    re.compile(r"!\[\[[^\]]+\]\]"),                           # image embeds
    re.compile(r"(?<![\\$])\$(?!\$)[^\n$]+?(?<![\\$])\$(?!\$)"),  # inline math
    re.compile(r"<!--.*?-->", re.S),                          # html comments
]


def protect(text: str) -> tuple[str, list[str]]:
    store: list[str] = []

    def put(m: re.Match) -> str:
        store.append(m.group(0))
        return token(len(store) - 1)

    for pat in PROTECT_PATTERNS:
        text = pat.sub(put, text)
    return text, store


def restore(text: str, store: list[str]) -> str:
    for _ in range(4):  # nested placeholders resolve over passes
        text = TOKEN_RE.sub(lambda m: store[int(m.group(1))], text)
    return text


def split_chunks(md: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Split on blank lines, never inside a $$ block or code fence."""
    blocks, buf, in_math, in_code = [], [], False, False
    lines = md.split("\n")
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith(("```", "~~~")):
            in_code = not in_code
        elif not in_code and s == "$$":
            in_math = not in_math
        buf.append(line)
        if not s and not in_math and not in_code and i < len(lines) - 1:
            blocks.append("\n".join(buf) + "\n")
            buf = []
    if buf:
        blocks.append("\n".join(buf))  # last block: no trailing newline, so chunks join losslessly

    chunks, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) > limit:
            chunks.append(cur)
            cur = ""
        cur += b
    if cur.strip():
        chunks.append(cur)
    return chunks


# ----------------------------------------------------------------------------
# Ollama
# ----------------------------------------------------------------------------
CLEAN_SYSTEM = """You clean Markdown that was machine-extracted from a PDF.
Rules:
- Fix only extraction damage: broken line wraps inside sentences, hyphenation split
  across lines, stray page headers/footers/page numbers, obvious OCR typos,
  duplicated lines, and wrong heading levels (use # for the title, ## sections, ### subsections).
- Tokens like \u27e6P3\u27e7 are protected content (math, tables, images). Keep every one,
  exactly as written, once, in the original order. Never write math yourself.
- Do NOT summarise, translate, reorder, add commentary or invent text.
- Output ONLY the cleaned Markdown, no code fence around it."""

LATEX_SYSTEM = """Transcribe the equation in the image to LaTeX.
Output ONLY the LaTeX body: no $ or $$ delimiters, no code fence, no explanation.
Use \\begin{aligned} ... \\end{aligned} with & and \\\\ for multi-line equations.
Use \\tag{n} for an equation number shown at the side."""

FIGURE_SYSTEM = """Describe this figure from a technical document in 1-3 factual sentences.
For a chart state the axes and the main trend; for a diagram name the components and flow.
If the image is only a logo, icon, decoration, page header or footer, or is mostly text, reply with exactly: SKIP
No speculation, no preamble, no markdown."""
MIN_FIGURE_PX = 100


def usable_description(d: str) -> str:
    """Empty string when the model declined (SKIP) or gave nothing useful."""
    d = " ".join(d.split())
    return "" if not d or d.upper().startswith("SKIP") else d


# Output caps (tokens): a runaway generation on a garbled crop must not tie up a slot for minutes.
MAX_OUT = {"latex": 900, "inline": 1400, "figure": 250, "clean": 3200}


def ollama_chat(host: str, model: str, system: str, user: str, timeout: int,
                images: list[bytes] | None = None, num_ctx: int = NUM_CTX, max_out: int | None = None) -> str:
    msg: dict = {"role": "user", "content": user}
    if images:
        msg["images"] = [base64.b64encode(i).decode() for i in images]
    body = json.dumps({
        "model": model, "stream": False, "think": False,
        "options": {"temperature": 0, "num_ctx": num_ctx, **({"num_predict": max_out} if max_out else {})},
        "messages": [{"role": "system", "content": system}, msg],
    }).encode()
    req = urllib.request.Request(f"{host}/api/chat", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["message"]["content"]


API_CHOICES = ("ollama", "openai")


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


class Backend:
    """Where a model lives: an Ollama server or an OpenAI-compatible one."""

    def __init__(self, api: str, host: str, model: str, key: str | None = None):
        self.api, self.host, self.model, self.key = api, host, model, key

    def chat(self, system: str | None, user: str, timeout: int, images: list[bytes] | None = None,
             num_ctx: int = NUM_CTX, max_out: int | None = None) -> str:
        if self.api == "openai":
            return openai_chat(self.host, self.model, system, user, timeout, images, max_out, self.key)
        return ollama_chat(self.host, self.model, system or "", user, timeout, images, num_ctx, max_out)

    def check(self) -> None:
        if self.api == "ollama":
            check_ollama(self.host, self.model)
            return
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{api_base(self.host)}/models", headers=headers), timeout=5):
                pass
        except urllib.error.HTTPError:
            pass   # the server answered; some do not implement /models
        except (urllib.error.URLError, OSError) as e:
            sys.exit(f"error: cannot reach the OpenAI-compatible server at {api_base(self.host)} ({e}).")

    def describe(self) -> str:
        return f"{self.api}:{self.model}"


def check_ollama(host: str, model: str) -> None:
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=5) as r:
            names = {m["name"] for m in json.load(r)["models"]}
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"error: cannot reach Ollama at {host} ({e}). Start it with `ollama serve`.")
    if model not in names and f"{model}:latest" not in names:
        sys.exit(f"error: model '{model}' not installed. Have: {', '.join(sorted(names))}")


def check_output(masked: str, out: str) -> str | None:
    """None if the model output is acceptable, else the reason it is not."""
    want = TOKEN_RE.findall(masked)
    got = TOKEN_RE.findall(out)
    if got != want:
        missing = len(set(want) - set(got))
        return f"placeholders changed ({missing} missing, {len(got)}/{len(want)} present)"
    ratio = len(out) / max(len(masked), 1)
    if not 0.6 <= ratio <= 1.3:
        return f"length ratio {ratio:.2f}"
    invented = {c for c in out if ord(c) >= 0x2E80} - set(masked)  # CJK/kana/hangul the page never had
    if invented:
        return f"introduced characters {''.join(sorted(invented))[:5]}"
    return None


MAX_PROTECTED = 24   # chunks denser than this in protected spans lose placeholders; not worth the calls


def clean_chunk(chunk: str, chat: Callable[[str], str], log: list[str],
                depth: int = 0) -> tuple[str, int]:
    """Clean one chunk. Returns (text, number of pieces kept uncleaned).

    Two attempts at full size; on failure split the chunk in halves and try each.
    """
    masked, store = protect(chunk)
    if len(TOKEN_RE.sub("", masked).strip()) < 40:  # nothing worth cleaning
        return chunk, 0
    n_tokens = len(TOKEN_RE.findall(masked))
    if n_tokens > MAX_PROTECTED:
        parts = split_chunks(chunk, max(MIN_SPLIT_CHARS, len(chunk) // 2))
        if len(parts) > 1:   # split first: smaller pieces hold fewer placeholders and usually succeed
            res, bad = [], 0
            for part in parts:   # same depth: density splits do not use up the failure-retry budget
                t, b = clean_chunk(part, chat, log, depth)
                res.append(t)
                bad += b
            return "".join(res), bad
        log.append(f"skipped: {n_tokens} protected spans")
        return chunk, 0
    reason = "no response"
    for _ in range(2 if depth == 0 else 1):
        try:
            out = chat(masked).strip()
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            reason = f"request failed ({e.__class__.__name__})"
            continue
        out = re.sub(r"^```(?:markdown|md)?\n|\n```$", "", out).strip()
        reason = check_output(masked, out) or ""
        if not reason:
            return restore(out, store) + "\n", 0
    log.append(f"{'  ' * depth}attempt failed: {reason}")
    if depth < 1 and len(chunk) > MIN_SPLIT_CHARS:
        parts = split_chunks(chunk, len(chunk) // 2 + 1)
        if len(parts) > 1:
            res, bad = [], 0
            for p in parts:
                t, b = clean_chunk(p, chat, log, depth + 1)
                res.append(t)
                bad += b
            return "".join(res), bad
    return chunk, 1


def clean_chunk_logged(chunk: str, chat: Callable[[str], str]) -> tuple[str, int, list[str]]:
    """Thread-friendly wrapper: returns (text, pieces kept, failure log)."""
    log: list[str] = []
    text, bad = clean_chunk(chunk, chat, log)
    return text, bad, log


# ----------------------------------------------------------------------------
# Deciding what needs the model, heading levels, checkpoint cache
# ----------------------------------------------------------------------------
LIGATURES = re.compile("[ﬀ-ﬆ]")
GARBAGE = re.compile("[�\x00-\x08\x0b-\x1f-]")
HYPHEN_WRAP = re.compile(r"[a-z]{2}-\n[a-z]{2}")
SOFT_WRAP = re.compile(r"[a-z,;]\n(?![\s\-*>#|0-9])[a-z]")
PAGE_NO = re.compile(r"^\s*(?:page\s+)?\d{1,4}\s*$", re.I | re.M)
DOUBLE_SPACE = re.compile(r"[A-Za-z,.] {2,}[A-Za-z]")


def damage_reasons(masked: str) -> list[str]:
    """Signs of extraction damage in (placeholder-masked) text; empty means leave it alone."""
    out = []
    for name, pat in (("ligature", LIGATURES), ("hyphenated line wrap", HYPHEN_WRAP),
                      ("mid-sentence line break", SOFT_WRAP), ("page number line", PAGE_NO),
                      ("double spaces", DOUBLE_SPACE)):
        if pat.search(masked):
            out.append(name)
    lines = [l.strip() for l in masked.split("\n") if len(l.strip()) > 12 and not l.startswith(("#", ">", "|"))]
    if any(v > 1 for v in Counter(lines).values()):
        out.append("repeated line")
    return out


def chunk_needs_cleaning(chunk: str, mode: str) -> bool:
    if mode == "none":
        return False
    if mode == "all":
        return True
    return bool(damage_reasons(protect(chunk)[0]))


AMP_ENTITY = re.compile(r"&amp;(?!(?:[A-Za-z][A-Za-z0-9]*|#\d+|#x[0-9A-Fa-f]+);)")


def unescape_entities(md: str) -> str:
    """Docling HTML-escapes '&' in text; undo that outside code (a literal &amp;nbsp; is left alone)."""
    out, in_code = [], False
    for line in md.split("\n"):
        if line.strip().startswith(("```", "~~~")):
            in_code = not in_code
        out.append(line if in_code else AMP_ENTITY.sub("&", line))
    return "\n".join(out)


NUMBERED_HEADING = re.compile(r"^#{1,6} +(\d+(?:\.\d+){0,4})\.? +(?=\S)", re.M)


CHAPTER_PAIR = re.compile(r"^#{1,6} Chapter (\d{1,3})[ \t]*\n+#{1,6} (\S[^\n]*)$", re.M)
HEADING = re.compile(r"^(#{1,6}) +(\S.*?)\s*$")
CAPTION_START = re.compile(r"(?:Figure|Fig\.|Table|Listing|Algorithm)\s+\d+(?:\.\d+)*[.:\s]", re.I)
CODE_HEADING = re.compile(r"\b(?:torch|np|numpy|plt|tf|nn|self)\.\w+|\bdef \w+\(|\bimport \w+|[;{}]\s*$")
TRAILING_PAGE = re.compile(r"^(.*\S)\s+\d{1,4}$")


def not_a_heading(text: str) -> bool:
    """Code comments, captions and whole sentences that extraction mistook for headings."""
    if len(text) > 120 or len(text) <= 1 or text.startswith(">") or CAPTION_START.match(text):
        return True
    if CODE_HEADING.search(text) or ("=" in text and re.search(r"[\[\](){}]", text)):
        return True
    return len(text) > 50 and bool(re.search(r"[a-z]\. [A-Z]", text))


def fix_headings(md: str) -> str:
    """Tidy the heading outline.

    Chapter N + title become one heading; numbered headings get the level their number implies
    (3 -> #, 3.4 -> ##); code lines, captions and sentences mistaken for headings become plain text;
    table-of-contents entries that repeat a real heading become plain text.
    """
    md = CHAPTER_PAIR.sub(lambda m: f"# {m.group(1)} {m.group(2).strip()}", md)
    md = re.sub(r"^#{1,6}[ \t]*\n", "", md, flags=re.M)     # empty headings: extraction left only the '#'
    lines = md.split("\n")
    in_code, heads = False, []
    for i, line in enumerate(lines):
        if line.strip().startswith(("```", "~~~")):
            in_code = not in_code
        m = None if in_code else HEADING.match(line)
        if m:
            heads.append((i, m.group(2)))
    texts = {t for _, t in heads}
    out_lines = list(lines)
    for i, t in heads:
        toc = TRAILING_PAGE.match(t)
        if not_a_heading(t) or (toc and toc.group(1) in texts and NUMBERED_HEADING.match(lines[i])):
            out_lines[i] = t                      # plain paragraph text
            continue
        m = NUMBERED_HEADING.match(lines[i])
        if m:
            level = min(6, m.group(1).count(".") + 1)
            out_lines[i] = "#" * level + lines[i][lines[i].index(" "):]
    return "\n".join(out_lines)


# ----------------------------------------------------------------------------
# Splitting a book into chapter notes
# ----------------------------------------------------------------------------
def normalize_pdf_outline(raw: list, page_count: int) -> list[dict]:
    """Ignore broken destination branches and publisher wrappers, not semantic levels.

    Page numbers are physical, one-based PDF pages, never printed page labels.
    Some PDFs contain a second outline tree whose destinations are all broken.
    """
    result, wrappers = [], []
    invalid_level = None
    previous_page = 0
    for level, title, page in raw:
        if invalid_level is not None:
            if level > invalid_level:
                continue
            invalid_level = None
        wrappers = [n for n in wrappers if n < level]
        title = " ".join(title.split())
        if not 1 <= page <= page_count:
            invalid_level = level
            continue
        if page < previous_page:  # stale/non-document-order destinations are unsafe anchors
            continue
        previous_page = page
        wrapper = re.fullmatch(r"(FM|Ch\d+|App|Notations|Index)_\d+P", title, re.I)
        if wrapper:
            wrappers.append(level)
            if wrapper[1].lower() not in ("app", "notations"):
                continue
            title = "Appendix" if wrapper[1].lower() == "app" else "Notations"
            depth = max(1, level - len(wrappers) + 1)
        else:
            if re.fullmatch(r"[A-Z]", title) or not title:
                continue
            depth = max(1, level - len(wrappers))
        entry = {"level": min(6, depth), "title": title, "page": page}
        if entry not in result:
            result.append(entry)
    return result


def read_pdf_outline(pdf: Path) -> list[dict]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    raw = []
    try:
        with pdf.open('rb') as stream:
            reader = PdfReader(stream)

            def walk(items: list, level: int = 1) -> None:
                for item in items:
                    if isinstance(item, list):
                        walk(item, level+1)
                    else:
                        try:
                            page = reader.get_destination_page_number(item)
                            page = page+1 if page is not None and page >= 0 else -1
                        except (PdfReadError, ValueError, KeyError, TypeError):
                            page = -1
                        raw.append([level, str(item.get('/Title', '')), page])

            walk(reader.outline)
            return normalize_pdf_outline(raw, len(reader.pages))
    except PdfReadError as exc:
        raise ValueError('unreadable PDF outline') from exc


def heading_key(text: str) -> tuple[str, str]:
    """Keep a section number separately so similar titles cannot change chapter identity."""
    text = re.sub(r"^#{1,6}\s+", "", text.strip())
    number = re.match(r"^(\d+(?:\.\d+)*|[A-Z](?:\.\d+)+)[ .\t]+", text)
    prefix = number[1] if number else ""
    if number:
        text = text[number.end():]
    text = unicodedata.normalize("NFKD", text).casefold()
    return prefix, "".join(c for c in text if c.isalnum())


def repair_pdf_headings(md: str, outline: list[dict]) -> tuple[str, dict]:
    """Conservative, auditable title matching using outline order and page locality.

    Only heading spans are changed. Equations, fenced code, links and prose remain
    untouched. Ambiguous/missing entries are reported, never inserted or guessed.
    Sparse historical page links bound candidates; new Paddle output has per-page
    comments even when visible PDF links are disabled.
    """
    lines = md.split("\n")
    protected, anchors = set(), []
    fence, math = None, False
    for i, line in enumerate(lines):
        stripped = line.strip()
        f = re.match(r"^(`{3,}|~{3,})", stripped)
        if f:
            protected.add(i)
            if fence is None:
                fence = f[1]
            elif f[1][0] == fence[0] and len(f[1]) >= len(fence):
                fence = None
            continue
        if fence:
            protected.add(i)
            continue
        if stripped == "$$":
            math = not math
            protected.add(i)
            continue
        if math:
            protected.add(i)
            continue
        a = re.search(r"<!-- pdf2md:page=(\d+) -->|\.pdf#page=(\d+)", line)
        if a:
            anchors.append((i, int(a[1] or a[2])))
    if not anchors or not outline:
        return md, {"matched": 0, "changes": [], "unmatched": outline, "ambiguous": [],
                    "reason": "no page anchors" if not anchors else "no usable PDF bookmarks"}

    # Linear-time page bounds; a nearby following link usually belongs to the heading.
    previous, following = [], []
    j, last = 0, None
    for i in range(len(lines)):
        while j < len(anchors) and anchors[j][0] <= i:
            last = anchors[j]
            j += 1
        previous.append(last)
    j, nxt = len(anchors)-1, None
    for i in range(len(lines)-1, -1, -1):
        while j >= 0 and anchors[j][0] >= i:
            nxt = anchors[j]
            j -= 1
        following.append(nxt)
    following.reverse()

    def local(i: int, page: int) -> bool:
        before, after = previous[i], following[i]
        # An explicit page comment anchors all following text until the next page.
        if before and "<!-- pdf2md:page=" in lines[before[0]]:
            return before[1] == page
        if after and after[0]-i <= 6:
            return abs(after[1]-page) <= 1
        bounds = [a[1] for a in (before, after) if a]
        return min(bounds)-1 <= page <= max(bounds)+1

    candidates = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if i in protected or not stripped or stripped.startswith(("[[", "!", "|", "<", "---")):
            continue
        is_heading = bool(re.match(r"^#{1,6} ", line))
        # Plain text may be a lost chapter title; never mine long prose paragraphs.
        if not is_heading and (len(stripped) > 180 or not (
                i == 0 or not lines[i-1].strip() or lines[i-1].startswith('<!-- pdf2md:page='))):
            continue
        spans, joined = [i], re.sub(r"^#{1,6} ", "", line).strip()
        candidates.append((i, tuple(spans), joined, is_heading))
        if not is_heading:
            continue
        # Join titles split into multiple headings, keeping intervening page links.
        for k in range(i+1, min(i+12, len(lines))):
            s = lines[k].strip()
            if not s or re.match(r"^\[\[.*\.pdf#page=", s) or s.startswith('<!-- pdf2md:page='):
                continue
            if k in protected or not re.match(r"^#{1,6} ", lines[k]):
                break
            if re.match(r"^#{1,6} (?:\d|[A-Z]\.\d)", lines[k]):
                break
            spans.append(k)
            joined += " " + re.sub(r"^#{1,6} ", "", lines[k]).strip()
            if len(joined) > 300 or len(spans) > 3:
                break
            candidates.append((i, tuple(spans), joined, True))

    edits, matched, missing, ambiguous = {}, 0, [], []
    structural = {}
    cursor = -1
    changes = []
    for entry in outline:
        number, key = heading_key(entry['title'])
        options = []
        for i, span, text, was_heading in candidates:
            if i <= cursor or not local(i, entry['page']):
                continue
            n, candidate_key = heading_key(text)
            if n and number and n != number:
                continue
            if n and not number and n.count('.') + 1 != entry['level']:
                continue
            if not was_heading and entry['level'] != 1:
                continue
            score = 1.0 if key == candidate_key else difflib.SequenceMatcher(None, key, candidate_key).ratio()
            if score < .94 or (score < 1 and min(len(key),len(candidate_key)) < 16):
                continue
            options.append((score, i, span, text))
        options.sort(key=lambda x: (-x[0], x[1]))
        if not options:
            missing.append(entry)
            continue
        best = options[0]
        if len(options) > 1 and best[0]-options[1][0] < .025:
            ambiguous.append(entry)
            continue
        _, i, span, text = best
        # Preserve extracted words/spelling and finer numbers: bookmarks can have
        # their own typos. Use canonical numbers only when extraction lost them.
        existing_number, _ = heading_key(text)
        title = text
        if number and not existing_number:
            title = number + ' ' + title
        replacement = '#' * entry['level'] + ' ' + title
        structural[i] = entry
        old = [lines[k] for k in span]
        if old != [replacement]:
            edits[i] = replacement
            for k in span[1:]:
                edits[k] = None
            changes.append({"line": i+1, "old": old, "new": replacement, "page": entry['page']})
        cursor = max(span)
        matched += 1
    # OCR often promotes callouts, subtitles and even code comments to H1/H2.
    # Within a positively identified chapter, these are subordinate to the most
    # recent numbered/bookmarked section, not new peers of that section.
    active, parent_depth, chapter_end = False, 1, None
    for i, line in enumerate(lines):
        if i in protected or edits.get(i, line) is None:
            continue
        if i in structural:
            e = structural[i]
            parent_depth = e['level']
            if parent_depth == 1:
                active = True
                chapter_end = next((x['page'] for x in outline
                                    if x['level'] == 1 and x['page'] > e['page']), None)
            continue
        h = re.match(r'^(#{1,6}) (.+)$', line)
        if not h or not active:
            continue
        n, _ = heading_key(h[2])
        if n:
            parent_depth = min(6, n.count('.') + 1)
            if parent_depth == 1:  # unmatched chapter: fall back rather than flatten it
                active = False
            continue
        if re.fullmatch(r'notes|problems|exercises|further reading|references|bibliography|summary', h[2], re.I):
            # Common unbookmarked end-of-chapter sections are chapter-level
            # siblings, not children of the preceding Summary or last section.
            parent_depth = 2
            continue
        before = previous[i]
        if chapter_end is not None and before and before[1] >= chapter_end:
            active = False
            continue
        depth = len(h[1])
        if depth <= parent_depth:
            replacement = '#' * min(6, parent_depth+1) + ' ' + h[2]
            if replacement != line:
                edits[i] = replacement
                changes.append({'line': i+1, 'old': [line], 'new': replacement,
                                'page': before[1] if before else None,
                                'reason': 'subordinate OCR heading inside a confirmed chapter'})
    return '\n'.join(edits.get(i, line) for i, line in enumerate(lines) if edits.get(i, line) is not None), {
        "matched": matched, "changes": changes, "unmatched": missing, "ambiguous": ambiguous}


CHAPTER_H1 = re.compile(r"^# (\d{1,3})[ \t]+(\S.*?)[ \t]*$")
BACK_HEADING = re.compile(r"^#{1,2} (bibliography|references|index|glossary)\s*$", re.I)
UNSAFE_NAME = re.compile(r'[\\/:*?"<>|#^\[\]]')


def safe_name(text: str, limit: int = 80) -> str:
    t = re.sub(r"\s+", " ", UNSAFE_NAME.sub(" ", text)).strip(" .")
    return (t[:limit].rstrip(" .") or "Untitled")


def find_chapters(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, number, title) of `# N Title` headings numbered 1, 2, 3 ... in order, outside code."""
    found, expected, in_code = [], 1, False
    for i, line in enumerate(lines):
        if line.strip().startswith(("```", "~~~")):
            in_code = not in_code
        m = None if in_code else CHAPTER_H1.match(line)
        if m and int(m.group(1)) == expected and not re.search(r"\s\d{1,4}$", m.group(2)):  # not a ToC entry
            found.append((i, expected, m.group(2)))
            expected += 1
    return found


def split_book(md: str, book: str) -> list[dict] | None:
    """Cut a book note into front matter, chapters and back matter. None if no chapter structure is found.

    Returns notes in reading order: {"file", "title", "kind", "chapter", "body"}. Nothing is dropped except the
    chapter heading lines, which are rewritten as each note's own H1.
    """
    lines = md.split("\n")
    chapters = find_chapters(lines)
    if len(chapters) < 2:
        return None
    width = max(2, len(str(chapters[-1][1])))
    last_start = chapters[-1][0]
    back: list[tuple[int, str]] = []                      # (line, name): sections after the last chapter
    in_code = False
    for i in range(last_start + 1, len(lines)):
        if lines[i].strip().startswith(("```", "~~~")):
            in_code = not in_code
        m = None if in_code else BACK_HEADING.match(lines[i])
        if m and (not back or back[-1][1].lower() != m.group(1).lower()):
            back.append((i, m.group(1).title()))
    cuts = [(i, "chapter", n, t) for i, n, t in chapters] + [(i, "back", 0, name) for i, name in back]
    cuts.sort()
    notes: list[dict] = []
    if cuts[0][0] > 0 and "\n".join(lines[:cuts[0][0]]).strip():
        notes.append({"title": "Front matter", "kind": "front", "chapter": None,
                      "body": "\n".join(lines[:cuts[0][0]]).strip("\n")})
    for k, (start, kind, n, title) in enumerate(cuts):
        end = cuts[k + 1][0] if k + 1 < len(cuts) else len(lines)
        body = "\n".join(lines[start + 1:end]).strip("\n")
        notes.append({"title": title, "kind": kind, "chapter": n or None, "body": body})
    used: set[str] = set()
    back_no = 90
    for note in notes:
        if note["kind"] == "front":
            stem = "00 Front matter"
        elif note["kind"] == "chapter":
            stem = f"{note['chapter']:0{width}d} {safe_name(note['title'])}"
        else:
            stem = f"{back_no} {safe_name(note['title'])}"
            back_no += 1
        while stem in used:
            stem += " (2)"
        used.add(stem)
        note["file"] = stem
    return notes


def render_note(note: dict, book: str, source_pdf: str, prev: str | None, nxt: str | None,
                index: str, date: str, warnings: int) -> str:
    heading = f"# {note['chapter']} {note['title']}" if note["chapter"] else f"# {note['title']}"
    nav = " | ".join(x for x in (f"[[{prev}|Previous]]" if prev else "", f"[[{index}|Contents]]",
                                  f"[[{nxt}|Next]]" if nxt else "") if x)
    front = (f"---\ntitle: \"{note['title'].replace(chr(34), chr(39))}\"\nbook: \"{book.replace(chr(34), chr(39))}\"\n"
             + (f"chapter: {note['chapter']}\n" if note["chapter"] else "")
             + f"source_pdf: \"{source_pdf}\"\nconverted: {date}\nmath_warnings: {warnings}\ntags: [pdf_import]\n---\n\n")
    return front + nav + "\n\n" + heading + "\n\n" + note["body"].strip("\n") + "\n"


def render_index(notes: list[dict], book: str, source_pdf: str, date: str) -> str:
    rows = []
    for n in notes:
        label = f"{n['chapter']}. {n['title']}" if n["chapter"] else n["title"]
        rows.append(f"- [[{n['file']}|{label}]]")
    return (f"---\ntitle: \"{book.replace(chr(34), chr(39))}\"\nsource_pdf: \"{source_pdf}\"\nconverted: {date}\n"
            f"tags: [pdf_import]\n---\n\n# {book}\n\n" + "\n".join(rows) + "\n")


def unique_dir(p: Path) -> Path:
    q, n = p, 2
    while q.exists():
        q, n = p.with_name(f"{p.name} ({n})"), n + 1
    return q


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
        tmp = self.root / f"{name}.json.tmp"
        tmp.write_text(json.dumps(obj), encoding="utf-8")
        tmp.replace(self.root / f"{name}.json")  # atomic: a crash never leaves a half-written entry


def cached_clean(cache: Cache, chunk: str, chat: Callable[[str], str], model: str):
    name = "clean_" + hashlib.sha1((model + CLEAN_SYSTEM + chunk).encode()).hexdigest()
    hit = cache.get(name)
    if hit:
        return hit["text"], hit["bad"], hit["log"]
    res = clean_chunk_logged(chunk, chat)
    cache.put(name, {"text": res[0], "bad": res[1], "log": res[2]})
    return res


def run_key(pdf: Path, *parts) -> str:
    st = pdf.stat()
    src = Path(__file__).read_bytes()  # any code change invalidates old checkpoints
    if WORKER.is_file():
        src += WORKER.read_bytes()
    raw = "|".join(map(str, (pdf, st.st_size, st.st_mtime_ns, hashlib.sha1(src).hexdigest(), *parts)))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def new_stats() -> dict:
    return {"eq_total": 0, "eq_tidied": 0, "eq_repaired": 0, "eq_unresolved": 0, "eq_as_text": 0, "eq_pictured": 0, "eq_flagged": 0,
            "figs_described": 0, "inline_total": 0, "inline_rewritten": 0, "inline_retried_ok": 0,
            "prose_restored": 0, "composite_figures": 0, "inline_source_restored": 0, "inline_source_images": 0,
            "running_headers": 0}


def merge_stats(a: dict, b: dict) -> dict:
    return {k: a.get(k, 0) + b.get(k, 0) for k in {**a, **b}}


def free_gpu_memory() -> None:
    """Best effort: release cached GPU memory so a retry after an out-of-memory error can succeed."""
    import gc
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


def convert_resilient(convert_fn: Callable[[tuple[int, int]], tuple[str, dict]],
                      rng: tuple[int, int], failed: list[int],
                      cleanup: Callable[[], None] | None = None) -> tuple[str, dict]:
    """Convert a slice; on failure retry in halves, retry a single page once, then mark it."""
    try:
        return convert_fn(rng)
    except Exception as e:  # noqa: BLE001 - a bad page must not kill a multi-hour run
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
            return f"<!-- page {lo} could not be converted ({e2.__class__.__name__}) -->\n", new_stats()


# ----------------------------------------------------------------------------
# Inline math: paragraphs whose maths survived only as flattened Unicode text
# ----------------------------------------------------------------------------
INLINE_SYSTEM = """You get an image of one paragraph from a technical document, plus the text extracted
from it (the extraction may have lost subscripts, superscripts, fractions and symbols).
Transcribe the paragraph from the IMAGE, keeping every word, but write each mathematical expression,
variable, symbol, subscript and superscript as inline LaTeX between single dollar signs, e.g. $x_i$ or
$\\sigma(K)=0$. Never use $$ or display math. Plain words stay outside the dollar signs, and so do
acronyms, abbreviations, names and units (write FA, IDET, GPU, km as plain text, not as math).
Output ONLY the paragraph text, no commentary."""

INLINE_STRICT_SYSTEM = """A previous transcription of this paragraph was rejected because it changed the words or broke the LaTeX.
You get the paragraph's image and the text extracted from it. Reproduce the extracted text EXACTLY: the same
words in the same order, nothing added, nothing dropped. The only change allowed is wrapping each mathematical
expression (variables, symbols, subscripts, superscripts) in single dollar signs as valid inline LaTeX, using
the image to recover symbols the extraction damaged. Every opening $ needs a closing $. Never use $$.
Acronyms, names and units stay plain text. Output ONLY the paragraph."""


def should_retry(why: str) -> bool:
    """A rejection is worth a second, stricter attempt unless the paragraph simply has no maths."""
    return not why.startswith("no math produced")


MATH_CHARS = re.compile("[≤≥≠≈∈∉⊂⊆⊃⊇∪∩∑∏∫∂"
                        "∇√∞±×÷⋅∘→←↔⇒⇔↦∀∃"
                        "∧∨¬⊗⊕⊙′″†‖∥⟨⟩⌈⌉⌊"
                        "⌋ℝℕℤℚℂΑ-Ωα-ωϑϕϖℓ★]")
MATH_HINT = re.compile(r"(?<![A-Za-z])[A-Za-z]\s*[=<>]\s*[\w(\\-]|(?<![A-Za-z])[A-Za-z]\w?\([A-Za-z0-9, ]{1,12}\)|\^")
SPAN = re.compile(r"(?<![\\$])\$(?!\$)([^\n$]+?)(?<![\\$])\$(?!\$)")


LOST_GLYPH = re.compile("[\ufffd\ue000-\uf8ff]")
GLYPH_NAMES = re.compile(r"\b(?:(?:short|long)?(?:right|left)arrow|mapsto|[lr]brace|[lc]dots|var(?:epsilon|phi|theta|rho)"
                         r"|subseteq|setminus|equiv|nabla)\b")
TWO_LETTER = re.compile(r"\b[A-Z][a-z]\b")
COMMON_TWO = {"In", "It", "An", "As", "If", "He", "We", "Is", "On", "Or", "No", "My", "Me", "So", "To", "Do", "Go",
              "Up", "Us", "Am", "At", "Be", "By", "Hi", "Oh", "Of", "Ok", "Ex", "Mr", "Dr", "Ms", "St", "Vs"}
INLINE_MODES = ("auto", "all", "off")


def looks_mathy(text: str, mode: str = "auto") -> bool:
    """Could this paragraph hold maths that plain extraction flattened?  'all' sends every real paragraph."""
    t = text.strip()
    lost = bool(LOST_GLYPH.search(t))          # accents/arrows that extraction replaced by U+FFFD or private-use glyphs
    if not (4 if lost else 20) <= len(t) <= 2500 or "$" in t:
        return False
    if lost:
        return True
    if mode == "all":
        return len(re.findall(r"[A-Za-z]{3,}", t)) >= 3
    if MATH_CHARS.search(t) or MATH_HINT.search(t) or GLYPH_NAMES.search(t):
        return True
    return sum(1 for w in TWO_LETTER.findall(t) if w not in COMMON_TWO) >= 2  # "Ha, Hb": squashed subscripts


TEXT_GROUP = re.compile(r"\\text\s*\{((?:[^{}]|\{[^{}]*\})*)\}")


def is_prose_formula(text: str) -> bool:
    """Docling sometimes wraps a whole sentence (with a little maths) as a display formula."""
    words = sum(len(re.findall(r"[A-Za-z]{3,}", g)) for g in TEXT_GROUP.findall(text))
    return words >= 6


def prose_hint(text: str) -> str:
    """Readable approximation of such a 'formula' to give the model as the extracted text."""
    t = TEXT_GROUP.sub(lambda m: m.group(1), text)
    return re.sub(r"\\(?:begin|end)\{[a-z*]+\}|\\\\|&|\\[a-zA-Z]+", " ", t)


def apply_swaps(md: str, swaps: dict[str, str]) -> str:
    """Put rewritten paragraphs in place of their markers.

    A prose "formula" is emitted as $$marker$$ on one line and loses that wrapper. Only spaces and tabs may
    sit between the dollars and the marker: matching across blank lines would eat the closing $$ of the
    equation before it and the opening $$ of the one after it.
    """
    for marker, new in swaps.items():
        md = re.sub(r"(?<![$\\])\$\$[ \t]*" + re.escape(marker) + r"[ \t]*\$\$(?!\$)", lambda _m, new=new: new, md)
        md = md.replace(marker, new)
    return md


def repair_latex(span: str) -> str:
    """Drop \\left/\\right (keeping the delimiters) when only their count is wrong."""
    if latex_problems(span) == ["\\left/\\right count differs"]:
        span = re.sub(r"\\(?:left|right)\s*\.", "", span)
        span = re.sub(r"\\(?:left|right)(?![A-Za-z])\s*", "", span)
    return span


def inline_problem(orig: str, new: str) -> str | None:
    """Why a model rewrite is unacceptable, or None. Words must survive and every span must be valid LaTeX."""
    new = " ".join(new.split())
    if not new:
        return "empty"
    new = SPAN.sub(lambda m: "$" + repair_latex(m.group(1)) + "$", new)
    if "$$" in new:
        return "display math"
    if new.count("$") % 2:
        return "odd number of $"
    spans = SPAN.findall(new)
    if not spans:
        return "no math produced"
    for x in spans:
        if (p := latex_problems(x)):
            return f"invalid LaTeX ({p[0]})"
    words = lambda t: re.findall(r"[a-z]{4,}", t.lower())
    ow, nw = words(orig), words(SPAN.sub(" ", new))
    if not 0.5 <= len(new) / max(len(orig), 1) <= 3.0:
        return "length changed too much"
    if ow:
        keep = set(nw) | set(words(" ".join(spans)))
        recall = sum(w in keep for w in ow) / len(ow)
        if recall < 0.8:
            return f"dropped words (recall {recall:.2f})"
    if nw:
        precision = sum(w in set(ow) for w in nw) / len(nw)
        if precision < 0.9:
            return f"invented words (precision {precision:.2f})"
    return None


def fallback_problem(para: str) -> str | None:
    """Looser check for a transcription of an unreadable 'formula': it must be clean, not necessarily maths."""
    para = " ".join(para.split())
    if len(para) < 3:
        return "empty"
    if "$$" in para or para.count("$") % 2:
        return "bad dollars"
    for x in SPAN.findall(para):
        if latex_problems(x):
            return "invalid LaTeX"
    return None


def link_target(pdf_name: str, page: int) -> str | None:
    """Obsidian link to a page of the source PDF, or None if the name cannot be linked safely."""
    if not pdf_name or re.search(r"[\[\]|#^]", pdf_name):
        return None
    return f"[[{pdf_name}#page={page}|p. {page}]]"


def wants_page_link(text: str, level: int | None) -> bool:
    """Real headings (not Chapter labels, captions, code, or table-of-contents lines) down to level 3."""
    t = text.strip()
    if len(t) < 2 or re.match(r"^Chapter \d{1,3}$", t, re.I) or not_a_heading(t) or TRAILING_PAGE.match(t):
        return False
    return level is None or level <= 3


TAG_RE = re.compile(r"\\tag\{[^{}]*\}")
OPEN_END = re.compile(r"(?:[=+\-*/<>]|\\(?:cdot|times|sum|prod|int|to|rightarrow|leftarrow|leq|geq|approx|equiv|left))\s*$")
EMPTY_GROUP = re.compile(r"[\^_]\{\s*\}|\\(?:frac|sqrt|text|mathrm|mathbf|operatorname)\s*\{\s*\}")


def equation_doubt(latex: str, width_pt: float | None = None) -> str | None:
    """Why valid-looking LaTeX may still be wrong (cut off, empty arguments, too short for its picture)."""
    body = TAG_RE.sub("", latex)
    body = re.sub(r"\\(?:begin|end)\{[A-Za-z*]+\}", " ", body)
    body = re.sub(r"(?:\\\\\s*)+$", "", body.strip()).strip()
    if not body:
        return "empty"
    if EMPTY_GROUP.search(body):
        return "has an empty group"
    if OPEN_END.search(body):
        return "ends in an operator, may be cut off"
    if width_pt and len(re.sub(r"\s", "", body)) < width_pt / 20:
        return "short for the space it takes, may be incomplete"
    return None


def flag_equation(latex: str, why: str, png: bytes, page: int | None, pdf_name: str, assets_dir: Path) -> str:
    """The equation followed by a collapsed callout holding its source picture, for checking against the PDF."""
    name = f"equation_{hashlib.sha1(png).hexdigest()[:12]}.png"
    assets_dir.mkdir(parents=True, exist_ok=True)
    (assets_dir / name).write_bytes(png)
    link = link_target(pdf_name, page) if page else None
    lines = [f"> [!warning]- Check this equation against the PDF ({why})", f"> ![[{name}]]"]
    if link:
        lines.append(f"> {link}")
    return f"$${latex}$$\n\n" + "\n".join(lines) + "\n"


def last_resort_formula(text: str, png: bytes | None, ask_inline: Callable[[bytes, str], str] | None,
                        assets_dir: Path) -> tuple[str, str | None]:
    """An equation nobody could decode: try it as a paragraph (it may be a prose note), else embed its picture."""
    if png is None:
        return "none", None
    if ask_inline:
        try:
            para = " ".join(ask_inline(png, prose_hint(text)).split())
        except (urllib.error.URLError, OSError, KeyError, ValueError):
            para = ""
        if para and fallback_problem(para) is None:
            return "paragraph", finalize_inline(para)
    name = f"equation_{hashlib.sha1(png).hexdigest()[:12]}.png"
    assets_dir.mkdir(parents=True, exist_ok=True)
    (assets_dir / name).write_bytes(png)
    return "picture", f"![[{name}]]"


def accept_inline(orig: str, new: str) -> bool:
    return inline_problem(orig, new) is None


ACRONYM_SPAN = re.compile(r"\$([A-Z]{3,}s?|[A-Z]{2}s|[A-Za-z]{4,})\$")   # acronyms and plain words (emphasis)


def finalize_inline(new: str) -> str:
    """The accepted paragraph: repairable spans fixed (as validated), acronyms and plain words back to text."""
    new = SPAN.sub(lambda m: "$" + repair_latex(m.group(1)) + "$", " ".join(new.split()))
    return ACRONYM_SPAN.sub(r"\1", new)


# ----------------------------------------------------------------------------
# Vision (equations + figures)
# ----------------------------------------------------------------------------
def png_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def strip_latex_reply(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^```(?:latex|tex)?\s*|\s*```$", "", s)
    s = re.sub(r"^\$\$|\$\$$", "", s.strip())
    s = re.sub(r"^\\\[|\\\]$", "", s.strip())
    return s.strip().strip("$").strip()


def formula_ok(text: str) -> bool:
    """True if the formula text needs no model help (tidy and structurally valid)."""
    t = tidy_block(text.strip()) if text.strip() else ""
    return bool(t) and not latex_problems(t)


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


def repair_formula(text: str, has_orig: bool, get_png: Callable[[], bytes | None],
                   ask: Callable[[bytes], str] | None) -> tuple[str, str]:
    """Return (new text, status): kept | tidied | repaired | unresolved."""
    text = text.strip()
    tidy = tidy_block(text) if text else ""
    if tidy and not latex_problems(tidy):
        return tidy, "kept" if tidy == text else "tidied"
    if ask is None:
        return (tidy if tidy else text), "unresolved"
    png = get_png()
    if png:
        try:
            cand = tidy_block(repair_latex(strip_latex_reply(ask(png))))
        except (urllib.error.URLError, OSError, KeyError, ValueError):
            cand = ""
        if cand and not latex_problems(cand):
            return cand, "repaired"
    return (tidy if tidy else text), "unresolved"


# ----------------------------------------------------------------------------
# Docling
# ----------------------------------------------------------------------------
def make_converter(ocr: bool, page_images: bool):
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.datamodel.settings import settings
    from docling.document_converter import DocumentConverter, PdfFormatOption

    settings.perf.elements_batch_size = 4   # formula/code batches: small, because Ollama shares the GPU
    opts = PdfPipelineOptions()
    opts.do_formula_enrichment = True     # equations -> LaTeX
    opts.do_table_structure = True
    opts.do_ocr = ocr
    opts.generate_picture_images = True
    opts.generate_page_images = page_images   # needed to crop equations
    opts.images_scale = 2.0
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})


def page_count(pdf: Path) -> int | None:
    try:
        import pypdfium2
        d = pypdfium2.PdfDocument(str(pdf))
        try:
            return len(d)
        finally:
            d.close()
    except Exception:
        return None


def convert_slice(conv, pdf: Path, page_range: tuple[int, int], assets_dir: Path,
                  pool: ThreadPoolExecutor | None,
                  ask_latex: Callable[[bytes], str] | None,
                  ask_figure: Callable[[bytes], str] | None,
                  ask_inline: Callable[[bytes, str], str] | None = None,
                  inline_mode: str = "auto",
                  ask_retry: Callable[[bytes, str], str] | None = None,
                  page_links: bool = False, flag_doubt: bool = True) -> tuple[str, dict]:
    """Convert one page slice. Equation re-reads, figure captions and inline-math rewrites run in parallel."""
    from docling_core.types.doc import (DocItemLabel, FormulaItem, ImageRefMode, PictureItem, SectionHeaderItem,
                                        TextItem)

    doc = conv.convert(str(pdf), page_range=page_range).document
    stats = new_stats()

    # pass 1: crop images here (PIL), start every model call at once
    formulas: list[tuple] = []
    pictures: list[Future | None] = []
    inlines: list[tuple] = []
    headings: list[tuple] = []
    for item, _ in doc.iterate_items(with_groups=False):
        if isinstance(item, FormulaItem):
            png, fut = None, None
            if pool and ask_inline and is_prose_formula(item.text or ""):
                im = item.get_image(doc)
                if im is not None:
                    hint = prose_hint(item.text or "")
                    png = png_bytes(im)
                    inlines.append((item, hint, pool.submit(ask_inline, png, hint), png, True))
                    continue
            if pool and ask_latex and not formula_ok(item.text or ""):
                im = item.get_image(doc)
                if im is not None:
                    png = png_bytes(im)
                    fut = pool.submit(ask_latex, png)
            formulas.append((item, png, fut))
        elif isinstance(item, PictureItem):
            fut = None
            if pool and ask_figure:
                im = item.get_image(doc)
                if im is not None and min(im.size) >= MIN_FIGURE_PX:
                    fut = pool.submit(ask_figure, png_bytes(im))
            pictures.append(fut)
        elif isinstance(item, SectionHeaderItem):
            headings.append((item, item.prov[0].page_no if item.prov else None, getattr(item, "level", None)))
        elif (pool and ask_inline and isinstance(item, TextItem)
              and item.label in (DocItemLabel.TEXT, DocItemLabel.LIST_ITEM, DocItemLabel.CAPTION)
              and looks_mathy(item.text or "", inline_mode)):
            im = item.get_image(doc)
            if im is not None:
                png = png_bytes(im)
                inlines.append((item, item.text, pool.submit(ask_inline, png, item.text), png, False))

    # pass 2: collect in document order
    swaps: dict[str, str] = {}  # markers survive Docling's markdown escaping; swapped in after export
    def answer(fut) -> str:
        try:
            return " ".join(fut.result().split())
        except (urllib.error.URLError, OSError, KeyError, ValueError):
            return ""

    def settle(n, item, orig, new) -> str | None:
        """Accept a rewrite (None) or return why it was rejected."""
        why = inline_problem(orig, new) if new else "no answer"
        if why is None:
            marker = f"PDFMDINLINE{n}X"
            swaps[marker] = finalize_inline(new)
            item.text = marker
            stats["inline_rewritten"] += 1
        return why

    def reject(why, item, png, is_formula) -> None:
        key = "rej:" + why.split(" (")[0]
        stats[key] = stats.get(key, 0) + 1
        if is_formula:
            formulas.append((item, png, None))   # back to the equation path, keeping its picture

    second: list[tuple] = []
    for n, (item, orig, fut, png, is_formula) in enumerate(inlines):
        stats["inline_total"] += 1
        why = settle(n, item, orig, answer(fut))
        if why is None:
            continue
        if ask_retry and pool and should_retry(why):
            second.append((n, item, orig, png, is_formula, pool.submit(ask_retry, png, orig)))
        else:
            reject(why, item, png, is_formula)
    for n, item, orig, png, is_formula, fut2 in second:   # one stricter attempt for the rejected ones
        why = settle(n, item, orig, answer(fut2))
        if why is None:
            stats["inline_retried_ok"] = stats.get("inline_retried_ok", 0) + 1
        else:
            reject(why, item, png, is_formula)
    keys = {"tidied": "eq_tidied", "repaired": "eq_repaired", "unresolved": "eq_unresolved"}
    for item, png, fut in formulas:
        stats["eq_total"] += 1
        new, status = repair_formula(
            item.text or "", bool(item.orig), lambda png=png: png,
            (lambda _p, fut=fut: fut.result()) if (ask_latex and fut is not None) else None)
        if status == "unresolved":
            kind, repl = last_resort_formula(item.text or "", png, ask_inline, assets_dir)
            if repl is not None:
                marker = f"PDFMDFORMULA{len(swaps)}X"
                swaps[marker] = repl
                item.text = marker
                stats["eq_as_text" if kind == "paragraph" else "eq_pictured"] += 1
                continue
        if status in ("tidied", "repaired") or (status == "unresolved" and new):
            item.text = new
        if status != "kept":
            stats[keys[status]] += 1
        if flag_doubt and status in ("kept", "tidied", "repaired"):
            why = equation_doubt(new, item.prov[0].bbox.width if item.prov else None)
            im = item.get_image(doc) if why else None
            if why and im is not None:       # keep the LaTeX, add a collapsed callout with the source picture
                marker = f"PDFMDFORMULA{len(swaps)}X"
                swaps[marker] = flag_equation(new, why, png_bytes(im), item.prov[0].page_no if item.prov else None,
                                              pdf.name, assets_dir)
                item.text = marker
                stats["eq_flagged"] += 1
    head_marks: list[tuple[str, int, str | None]] = []
    for n, (item, page, level) in enumerate(headings):    # a link to the PDF page under each real heading
        link = link_target(pdf.name, page) if page and page_links and wants_page_link(item.text or "", level) else None
        if page:
            marker = f"PDFMDHEAD{n}X"
            head_marks.append((marker, page, link))
            item.text = f"{marker} {item.text}"
    descriptions: dict[int, str] = {}
    for n, fut in enumerate(pictures):
        if fut is None:
            continue
        try:
            d = usable_description(fut.result())
        except (urllib.error.URLError, OSError, KeyError, ValueError):
            d = ""
        if d:
            descriptions[n] = d
            stats["figs_described"] += 1

    with tempfile.TemporaryDirectory() as tmp:
        tmp_md = Path(tmp) / "out.md"
        doc.save_as_markdown(tmp_md, image_mode=ImageRefMode.REFERENCED, artifacts_dir=Path("art"))
        md = tmp_md.read_text(encoding="utf-8")
        art = Path(tmp) / "art"
        if art.is_dir() and any(art.iterdir()):
            assets_dir.mkdir(parents=True, exist_ok=True)
            for f in art.iterdir():
                shutil.copy2(f, assets_dir / f.name)
    md = apply_swaps(md, swaps)
    for marker, page, link in head_marks:
        md = re.sub(rf"^(#{{1,6}}) {marker} ?(.*)$",
                    lambda m, page=page, link=link: f"<!-- pdf2md:page={page} -->\n\n{m.group(1)} {m.group(2)}" +
                    (f"\n\n{link}" if link else ""), md, flags=re.M)

    def embed(m: re.Match) -> str:
        target = m.group(1)
        if target.startswith(("http://", "https://", "data:")):
            return m.group(0)
        from urllib.parse import unquote
        name = Path(unquote(target)).name
        out = f"![[{name}]]"
        n = re.match(r"image_(\d+)_", name)
        if n and int(n.group(1)) in descriptions:
            out += f"\n\n{FIGURE_CALLOUT}\n> {descriptions[int(n.group(1))]}\n"
        return out

    return re.sub(r"!\[[^\]]*\]\(([^)]+)\)", embed, md), stats


# ----------------------------------------------------------------------------
# PaddleOCR-VL engine: Paddle's layout model + a 0.9B VLM served by llama.cpp (no Ollama)
# ----------------------------------------------------------------------------
WORKER = Path(__file__).with_name("paddle_worker.py")


def source_verified_prose(native: str, recognized: str) -> str | None:
    """Prefer a well-aligned native prose block to a VLM's invented completion.

    Never use the text layer to reconstruct mathematics, code or scanned text.
    High lexical overlap and sane Unicode are required; doubtful blocks retain OCR.
    """
    if '$' in recognized or '\\' in recognized or len(native.strip()) < 40:
        return None
    if any(unicodedata.category(c) in ('Co', 'Cs', 'Cn') or c == '\ufffd' or
           (unicodedata.category(c) == 'Cc' and c not in '\r\n\t') for c in native):
        return None
    native = native.replace('\u00ad', '')
    def unwrap(m: re.Match) -> str:
        combined = m[1] + m[2]
        return combined if combined.casefold() in recognized.casefold() else m[1] + '-' + m[2]
    native = re.sub(r'([A-Za-z]+)-\s*\n\s*([a-z]+)', unwrap, native.replace('\r\n', '\n'))
    native = ' '.join(native.split())
    words = lambda t: Counter(re.findall(r'[^\W\d_]+', t.casefold()))
    src, ocr = words(native), words(recognized)
    a, b = sum(src.values()), sum(ocr.values())
    if min(a,b) < 8 or not .65 <= len(native)/max(1,len(recognized)) <= 1.35:
        return None
    overlap = sum((src & ocr).values())
    if overlap/max(a,b) < .75 or overlap/min(a,b) < .88:
        return None
    return native


def composite_figure_groups(blocks: list, page_height: int) -> list[tuple[list, list[int]]]:
    """Conservative mixed-panel figures ending at a caption, not ordinary tables.

    A run of non-prose drawing/table/formula blocks beneath one caption is kept
    as one source crop. This prevents panel fragments masquerading as equations.
    """
    groups = []
    ignored = {'header', 'footer', 'number', 'aside_text', 'header_image', 'footer_image'}
    panels = {'image','chart','table','display_formula','inline_formula','algorithm','formula_number'}
    for i, caption in enumerate(blocks):
        if caption.label != 'figure_title' or not re.match(r'^Fig(?:ure|\.)?\s*\d', caption.content, re.I):
            continue
        group = []
        for b in reversed(blocks[:i]):
            if b.label in ignored:
                continue
            if b.label not in panels or b.bbox[3] > caption.bbox[1]+3:
                break
            if group and min(x.bbox[1] for x in group)-b.bbox[3] > page_height*.10:
                break
            if not group and caption.bbox[1]-b.bbox[3] > page_height*.10:
                break
            group.append(b)
        if len(group) < 3 or not any(b.label in ('image','chart') for b in group):
            continue
        if len({b.label for b in group}) < 2:
            continue
        group.reverse()
        bbox = [min(b.bbox[0] for b in group), min(b.bbox[1] for b in group),
                max(b.bbox[2] for b in group), max(b.bbox[3] for b in group)]
        groups.append((group,bbox))
    return groups


def formula_body(raw: str) -> str:
    """Unwrap a single model math wrapper before adding our own display delimiters."""
    raw = raw.strip()
    for _ in range(4):
        for opening,closing in (('$$','$$'),(r'\[',r'\]'),(r'\(',r'\)')):
            if raw.startswith(opening) and raw.endswith(closing):
                raw = raw[len(opening):-len(closing)].strip()
                break
        else:
            break
    return tidy_block(raw)


def formula_retry_problems(candidate: str, native: str = '') -> list[str]:
    issues = latex_problems(candidate) if candidate else ['empty answer']
    issues += formula_delimiter_problems(candidate)
    if re.search(r'\\[\[\]()]',candidate):
        issues.append('nested math delimiters')
    if candidate.startswith('```') or not re.search(r'[=+_^{}\\]',candidate):
        issues.append('not a formula')
    # Digits survive font encoding better than mathematical letters. Reject new
    # numerical values absent from the same source rectangle, not a benchmark key.
    values = lambda s: Counter(re.findall(r'\d+(?:\.\d+)?',unicodedata.normalize('NFKC',s)))
    source_values = values(native)
    if source_values and len(native.strip()) >= 10:
        if any(n not in source_values for n in values(candidate)):
            issues.append('numerical value absent from native source crop')
        if any(n not in values(candidate) for n in source_values):
            issues.append('numerical value omitted from native source crop')
    issues += native_matrix_problems(candidate,native)
    issues += native_greek_problems(candidate,native)
    issues += native_operator_problems(candidate,native)
    issues += native_variable_problems(candidate,native)
    return issues


def native_operator_problems(tex: str, native: str) -> list[str]:
    """Preserve clearly decoded source operators, not a visually similar substitute."""
    native = unicodedata.normalize('NFKC',native)
    if len(native.strip()) < 10:
        return []
    aliases = {'∝':('propto',),'∞':('infty',),'≈':('approx',),'≠':('neq','ne'),
               '≤':('le','leq'),'≥':('ge','geq'),'∈':('in',),'∉':('notin',),
               '∂':('partial',),'∫':('int','iint','iiint'),'∑':('sum',),'∏':('prod',)}
    commands = set(re.findall(r'\\([A-Za-z]+)',tex))
    for glyph,names in aliases.items():
        if glyph in native and glyph not in tex and not commands.intersection(names):
            return ['mathematical operator omitted or substituted relative to native source crop']
    return []


def native_variable_problems(tex: str, native: str) -> list[str]:
    """Check retry variable multiplicities/case only when native glyphs are usable.

    This is a rejection guard, not a TeX reconstruction algorithm. Font ambiguity
    disables it, while a mismatch falls back to source pixels instead of guessing.
    """
    native = unicodedata.normalize('NFKC',native)
    if len(native.strip()) < 10 or len(re.findall('[A-Za-z]',native)) < 3:
        return []
    for c in native:
        if unicodedata.category(c) in ('Co','Cs','Cn') or c == '\ufffd':
            return []
        if c.isalpha() and not any(x in unicodedata.name(c,'') for x in ('LATIN','GREEK')):
            return []
    tex = re.sub(r'\\begin\{[^{}]+\}(?:\{[clr| ]+\})?|\\end\{[^{}]+\}', '',tex)
    tex = re.sub(r'\\tag\*?\{[^{}]*\}', '',tex)
    named = {'lim','log','ln','sin','cos','tan','exp','max','min','argmax','argmin','det','Pr',
             'sinh','cosh','tanh','arcsin','arccos','arctan','gcd','sup','inf'}
    tex = re.sub(r'\\([A-Za-z]+)',lambda m:m[1] if m[1] in named else '',tex)
    if Counter(re.findall('[A-Za-z]',tex)) != Counter(re.findall('[A-Za-z]',native)):
        return ['variable letters or multiplicities differ from native source crop']
    return []


def native_greek_problems(tex: str, native: str) -> list[str]:
    """A probability p must not silently become phi when native glyphs are clear."""
    native = unicodedata.normalize('NFKC',native)
    if len(native.strip()) < 10 or len(re.findall('[A-Za-z]',native)) < 3:
        return []
    for c in native:
        if unicodedata.category(c) in ('Co','Cs','Cn') or c == '\ufffd':
            return []
        if c.isalpha() and not any(x in unicodedata.name(c,'') for x in ('LATIN','GREEK')):
            return []  # mathematical fonts sometimes decode as unrelated scripts
    canonical = lambda s:s.translate(str.maketrans({'ϕ':'φ','ϵ':'ε','ϑ':'θ','ϖ':'π','ς':'σ'}))
    greek = {k:canonical(unicodedata.normalize('NFKC',v)) for k,v in TEXT_MACROS.items()
             if v.isalpha() and 'GREEK' in unicodedata.name(unicodedata.normalize('NFKC',v),'')}
    greek.update({'varphi':'φ','varepsilon':'ε','vartheta':'θ','varpi':'π','varsigma':'σ'})
    expected = canonical(native)
    if any(greek[name] not in expected for name in re.findall(r'\\([A-Za-z]+)',tex) if name in greek):
        return ['Greek symbol absent from native source crop']
    return []


def native_matrix_problems(tex: str, native: str) -> list[str]:
    """Repeated constants matter: [1,1,1,1] is not the source vector [1,1,1].

    A native text layer is evidence, not a source of replacement TeX. On a
    reliable numeric mismatch we retry/crop; we never invent a corrected matrix.
    """
    if not re.search(r'\\begin\{(?:[bpvBV]?matrix|array|aligned)\}',tex):
        return []
    values = lambda s: Counter(re.findall(r'\d+(?:\.\d+)?',unicodedata.normalize('NFKC',s)))
    source_values = values(native)
    if sum(source_values.values()) < 3:
        return []
    # Layout commands and equation labels are not entries of the matrix.
    tex = re.sub(r'\\(?:tag|hspace|vspace|rule)\*?\{[^{}]*\}', '',tex)
    if values(tex) != source_values:
        return ['matrix numerical multiplicities differ from native source crop']
    return []


def inline_math_problems(text: str) -> list[str]:
    issues = []
    if len(re.findall(r'(?<!\\)\$',text)) % 2:
        issues.append('unmatched inline dollar')
    for match in DOLLAR_INLINE.finditer(text):
        issues.extend(latex_problems(match[1]))
        issues.extend(formula_delimiter_problems(match[1]))
    return sorted(set(issues))


def formula_delimiter_problems(tex: str) -> list[str]:
    """Catch extra literal parentheses that TeX renders without complaining.

    Explicit one-sided delimiters and conventional mixed interval brackets are
    allowed. Only source preservation, not blind punctuation deletion, follows.
    """
    issues = []
    # A rectangular numeric array inside single determinant bars is ambiguous:
    # OCR often confuses a thin square bracket with a vertical bar. Re-read or
    # preserve the source; do not silently substitute a mathematical operator.
    pattern = r'\\left\s*\|\s*\\begin\{(array|matrix)\}(?:\{[clr|]+\})?(.*?)\\end\{\1\}'
    for match in re.finditer(pattern,tex,re.S):
        body = match[2]
        if '\\begin' in body:
            continue
        rows = [row for row in re.split(r'\\\\',body) if row.strip()]
        widths = [row.count('&')+1 for row in rows]
        if len(rows)>1 and len(set(widths))==1 and widths[0]!=len(rows):
            issues.append('ambiguous rectangular matrix inside determinant bars')
    tex = blank_text_groups(tex)
    tex = re.sub(r'\\(?:left|right|[Bb]ig(?:g)?[lr]?)\s*[\[\]().|]', '', tex)
    p = tex.count('(')-tex.count(')')
    b = tex.count('[')-tex.count(']')
    if p == -b and abs(p) == 1:
        return issues  # (a,b] or [a,b)
    if p or b:
        issues.append('unbalanced literal parentheses or brackets')
    return issues


def protect_paddle_result(result, native_text: Callable, crop: Callable, retry_formula: Callable | None = None,
                          page_no: int | None = None, outline: list[dict] | None = None,
                          repeated_margin: Callable | None = None) -> dict:
    """Source-backed guards before Paddle exports Markdown. No benchmark-specific rules.

    Failed formula recovery becomes an exact source image, never guessed LaTeX.
    Reports retain original/retry text and reasons so image fallbacks are visible.
    """
    blocks = result['parsing_res_list']
    height = result['height']
    if isinstance(height,list):
        height = height[0]
    report = {'prose_restored': [], 'composite_figures': [], 'formula_retries': [], 'formula_images': [],
              'inline_restored': [], 'inline_images': [], 'running_headers': []}
    removed = set()
    if page_no and outline and repeated_margin:
        for b in blocks:
            if b.label != 'paragraph_title' or b.bbox[3] > height*.065 or b.bbox[3]-b.bbox[1] > height*.025:
                continue
            number,key = heading_key(b.content)
            prior = [e for e in outline if e['page'] < page_no and heading_key(e['title'])[1] == key
                     and (not number or not heading_key(e['title'])[0] or heading_key(e['title'])[0] == number)]
            if prior and repeated_margin(b.content):
                removed.add(id(b))
                report['running_headers'].append({'bbox':b.bbox,'before':b.content,'section_page':prior[0]['page']})
    for group,bbox in composite_figure_groups(blocks,height):
        first = group[0]
        image = crop(bbox, 18)
        first.label, first.content, first.bbox = 'image', '', bbox
        first.image = {'path': f'imgs/source_figure_{blocks.index(first):04d}.png', 'img': image}
        removed.update(id(b) for b in group[1:])
        report['composite_figures'].append({'bbox':bbox,'panels':len(group),'image':first.image['path']})
    for i,b in enumerate(blocks):
        if id(b) in removed:
            continue
        if b.label in ('text','figure_title'):
            native = native_text(b.bbox)
            inline_issues = inline_math_problems(b.content)
            if inline_issues:
                restored = source_verified_prose(native,b.content.replace('$',''))
                record = {'bbox':b.bbox,'before':b.content,'problems':inline_issues}
                if restored:
                    b.content = restored.replace('$',r'\$')
                    record['after'] = b.content
                    report['inline_restored'].append(record)
                else:
                    b.label,b.content = 'image',''
                    b.image = {'path':f'imgs/source_inline_{i:04d}.png','img':crop(b.bbox,6)}
                    record['image'] = b.image['path']
                    report['inline_images'].append(record)
                continue
            restored = source_verified_prose(native,b.content)
            if restored and restored != ' '.join(b.content.split()):
                report['prose_restored'].append({'bbox':b.bbox,'before':b.content,'after':restored})
                b.content = restored
        if b.label != 'display_formula':
            continue
        original = b.content
        tex = formula_body(original)
        source = native_text(b.bbox)
        problems = (latex_problems(tex) + formula_delimiter_problems(tex) + native_matrix_problems(tex,source)
                    + native_greek_problems(tex,source) + native_operator_problems(tex,source))
        if not problems:
            continue
        replacement = None
        retry_log = []
        # Dense over/underbrace annotations are diagrams of mathematics. Even a
        # syntactically valid retry can move labels or change arguments silently.
        annotated = bool(re.search(r'\\(?:overbrace|underbrace|overset|underset)\b',tex))
        ambiguous_notation = 'ambiguous rectangular matrix inside determinant bars' in problems
        uncertain_operator = 'mathematical operator omitted or substituted relative to native source crop' in problems
        if retry_formula and not annotated and not ambiguous_notation and not uncertain_operator:
            for pad in (6,18):
                try:
                    raw = retry_formula(b.bbox,pad)
                    candidate = formula_body(raw)
                    issues = formula_retry_problems(candidate,source)
                    retry_log.append({'text':candidate,'problems':issues})
                    if not issues:
                        replacement = candidate
                        break
                except (OSError,ValueError,KeyError,RuntimeError) as exc:
                    retry_log.append({'error':type(exc).__name__})
        record = {'bbox':b.bbox,'before':original,'problems':problems,'attempts':retry_log}
        if annotated:
            record['reason'] = 'complex mathematical annotation preserved as source pixels'
        elif ambiguous_notation:
            record['reason'] = 'ambiguous matrix delimiter preserved as source pixels'
        elif uncertain_operator:
            record['reason'] = 'uncertain mathematical operator preserved as source pixels'
        if replacement is not None:
            b.content = '$$\n' + replacement + '\n$$'
            record['after'] = b.content
            report['formula_retries'].append(record)
        else:
            b.label,b.content = 'image',''
            b.image = {'path':f'imgs/source_formula_{i:04d}.png','img':crop(b.bbox,6)}
            record['image'] = b.image['path']
            report['formula_images'].append(record)
    result['parsing_res_list'] = [b for b in blocks if id(b) not in removed]
    return report


class _TableParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.cell: list[str] | None = None
        self.spans = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th"):
            self.cell = []
            if any(str(a.get(k) or "1").strip() not in ("", "1") for k in ("rowspan", "colspan")):
                self.spans = True
        elif tag == "br" and self.cell is not None:
            self.cell.append("<br>")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.rows:
            self.rows[-1].append(table_cell_breaks(" ".join("".join(self.cell).split())))
            self.cell = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def html_table_to_md(table_html: str) -> str | None:
    """A Markdown table for a simple HTML table, or None when it has merged cells (kept as HTML)."""
    t = _TableParser()
    t.feed(table_html)
    rows = [r for r in t.rows if r]
    if t.spans or not rows:
        return None
    width = max(len(r) for r in rows)
    esc = lambda c: c.replace("|", "\\|")
    rows = [[esc(c) for c in r] + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def table_cell_breaks(text: str) -> str:
    """Recover escaped bullet line breaks, not TeX macros or file paths."""
    return re.sub(r"\\n(?=\s*(?:[•●▪]|[-*]\s))", "<br>", text)


# Bare LaTeX in running text and table cells ("250 \mu M", "\pm 5") is shown literally by Obsidian: use the characters.
TEXT_MACROS = {
    "mu": "\u00b5", "alpha": "\u03b1", "beta": "\u03b2", "gamma": "\u03b3", "delta": "\u03b4", "epsilon": "\u03b5",
    "theta": "\u03b8", "lambda": "\u03bb", "pi": "\u03c0", "rho": "\u03c1", "sigma": "\u03c3", "tau": "\u03c4",
    "phi": "\u03c6", "omega": "\u03c9", "Delta": "\u0394", "Omega": "\u03a9", "Sigma": "\u03a3", "Gamma": "\u0393",
    "pm": "\u00b1", "times": "\u00d7", "cdot": "\u00b7", "le": "\u2264", "leq": "\u2264", "ge": "\u2265", "geq": "\u2265",
    "neq": "\u2260", "approx": "\u2248", "degree": "\u00b0", "circ": "\u00b0",
}
_MACRO = re.compile(r"\\(" + "|".join(sorted(TEXT_MACROS, key=len, reverse=True)) + r")(?![A-Za-z])( (?=[A-Za-z]))?")
_ESCAPED = re.compile(r"\\([%&_#])")
_MATH_SPAN = re.compile(r"(\$\$.*?\$\$|(?<![\\$])\$(?!\$)[^\n$]+?(?<![\\$])\$(?!\$))", re.S)


def _macro_text(m: re.Match) -> str:
    # TeX swallows the space after a control word; keep that only for units ("250 \\mu M" -> "250 µM")
    return TEXT_MACROS[m.group(1)] + (" " if m.group(2) and m.group(1) != "mu" else "")


def text_macros_to_unicode(md: str) -> str:
    """Replace bare LaTeX macros outside math and code with the characters they stand for."""
    out, in_code = [], False
    for line in md.split("\n"):
        if line.strip().startswith(("```", "~~~")):
            in_code = not in_code
        if in_code or "\\" not in line:
            out.append(line)
            continue
        parts = _MATH_SPAN.split(line)               # odd items are the math spans, untouched
        for i in range(0, len(parts), 2):
            parts[i] = _ESCAPED.sub(r"\1", _MACRO.sub(_macro_text, parts[i]))
        out.append("".join(parts))
    return "\n".join(out)


SUBSCRIPTS = dict(zip("0123456789+-=()aehijklmnoprstuvx", "\u2080\u2081\u2082\u2083\u2084\u2085\u2086\u2087\u2088\u2089\u208a"
                      "\u208b\u208c\u208d\u208e\u2090\u2091\u2095\u1d62\u2c7c\u2096\u2097\u2098\u2099\u2092\u209a\u1d63\u209b"
                      "\u209c\u1d64\u1d65\u2093"))
SUPERSCRIPTS = dict(zip("0123456789+-=()nirdths", "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079\u207a\u207b\u207c"
                        "\u207d\u207e\u207f\u2071\u02b3\u1d48\u1d57\u02b0\u02e2"))
_SCRIPT = re.compile(r"([_^])(?:\{([^{}]{1,4})\}|([A-Za-z0-9]))")
_SCRIPTED = re.compile(r"(?P<base>[A-Z]{1,4}|\d+)?\s*(?P<scripts>(?:[_^](?:\{[^{}]{1,4}\}|[A-Za-z0-9])\s*)+)(?P<tail>[A-Z][a-z]?)?")
_LONE_MACRO = re.compile(r"\\(\w+)(?:\s*([0-9]+(?:\.[0-9]+)?))?")


def simplify_inline_math(body: str) -> str | None:
    """Plain characters for inline maths that is really typography (CO2, 10^-3, 3rd, a lone plus-minus sign).

    Deliberately narrow: ordinary maths such as K_k, H_k, x_i or B^4 returns None and stays LaTeX.
    """
    b = body.strip()
    m = _LONE_MACRO.fullmatch(b)
    if m and m.group(1) in TEXT_MACROS:
        return TEXT_MACROS[m.group(1)] + (f" {m.group(2)}" if m.group(2) else "")
    m = _SCRIPTED.fullmatch(b)
    if not m:
        return None
    base, tail = m.group("base") or "", m.group("tail") or ""
    out = []
    for kind, braced, bare in _SCRIPT.findall(m.group("scripts")):
        arg = braced or bare
        table = SUBSCRIPTS if kind == "_" else SUPERSCRIPTS
        if not arg or any(c not in table for c in arg):
            return None
        if base and re.fullmatch(r"\d+", base):
            if kind != "^":                         # 10_2 is not typography
                return None
        elif len(base) == 1:                        # H_2, N_2, O_2: a digit subscript on one capital letter
            if kind != "_" or not arg.isdigit():
                return None
        elif len(base) >= 2:                        # CO_2, NO_x: a chemical formula, digit or x subscript
            if kind != "_" or not (arg.isdigit() or arg == "x"):
                return None
        elif kind == "^" and not arg.isalpha() and not arg.lstrip("+-").isdigit():
            return None
        out.append("".join(table[c] for c in arg))
    return base + "".join(out) + tail


def simplify_light_math(md: str) -> str:
    """Apply simplify_inline_math to every single-dollar span outside code; display maths is never touched."""
    out, in_code = [], False
    for line in md.split("\n"):
        if line.strip().startswith(("```", "~~~")):
            in_code = not in_code
        if in_code or "$" not in line:
            out.append(line)
            continue
        parts = _MATH_SPAN.split(line)
        for i in range(1, len(parts), 2):
            if not parts[i].startswith("$$"):
                plain = simplify_inline_math(parts[i][1:-1])
                if plain is not None:
                    parts[i] = plain
                    if all(c in SUBSCRIPTS.values() or c in SUPERSCRIPTS.values() for c in plain):
                        parts[i - 1] = parts[i - 1].rstrip(" ")      # "SO $_2$" -> "SO\u2082", "3 $^{rd}$" -> "3\u02b3\u1d48"
        out.append("".join(parts))
    return "\n".join(out)


_TABLE = re.compile(r"<table\b.*?</table>", re.S | re.I)
_DIV = re.compile(r"<div\b[^>]*>\s*(.*?)\s*</div>", re.S | re.I)
_IMG = re.compile(r"<img\b[^>]*?\bsrc=[\"']([^\"']+)[\"'][^>]*>", re.I)


def clean_paddle_markdown(md: str, page_no: int, src_dir: Path | None, assets_dir: Path | None,
                          link: str | None = None) -> str:
    """Paddle's page Markdown -> Obsidian Markdown: simple HTML tables become Markdown tables, caption wrappers
    go, <img> becomes an ![[embed]] (the picture is copied to the assets folder), headings get a PDF page link."""
    md = _TABLE.sub(lambda m: "\n\n" + (html_table_to_md(m.group(0)) or m.group(0)) + "\n\n", md)
    md = '\n'.join(table_cell_breaks(line) if line.lstrip().startswith('|') else line for line in md.split('\n'))
    md = _DIV.sub(lambda m: m.group(1), md)

    def img(m: re.Match) -> str:
        src = m.group(1)
        if src_dir is None or assets_dir is None or not (src_dir / src).is_file():
            return ""
        name = f"p{page_no:04d}_{Path(src).name}"
        assets_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_dir / src, assets_dir / name)
        return f"![[{name}]]"

    md = simplify_light_math(text_macros_to_unicode(_IMG.sub(img, md)))
    out, in_code = [], False
    for line in md.split("\n"):
        if line.strip().startswith(("```", "~~~")):
            in_code = not in_code
        elif not in_code and not line.lstrip().startswith("|"):
            line = re.sub(r"(?<=\S)  +(?=\S)", " ", line)         # "Two  $x$-moves" -> one space
        out.append(line)
        m = None if (in_code or not link) else HEADING.match(line)
        if m and wants_page_link(m.group(2), len(m.group(1))):
            out += ["", link]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip("\n") + "\n"


class ServerGuard:
    """Make sure an OpenAI-compatible server (llama-server) answers; start it with `command` if it does not,
    and stop it again afterwards if this run started it."""

    def __init__(self, url: str, command: str | None, wait: int = 180):
        self.url, self.command, self.wait = url, command, wait
        self.proc: subprocess.Popen | None = None
        self.log = Path(tempfile.gettempdir()) / "pdf2md-llama-server.log"

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
                     f"--port 8111 --temp 0 -ngl 99\nor pass --serve-command (see serve-paddleocr-vl.sh).")
        print(f"      starting the model server: {self.command}", flush=True)
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
                self.proc.kill()
        self.proc = None


def worker_command(pdf: Path, rng: tuple[int, int], server: str, out: Path, scale: float = 2.5) -> list[str]:
    return ["uv", "run", "--python", "3.12", str(WORKER), "--pdf", str(pdf), "--first", str(rng[0]),
            "--last", str(rng[1]), "--server", server, "--out", str(out), "--scale", str(scale)]


def paddle_slice(pdf: Path, page_range: tuple[int, int], assets_dir: Path, server: str, page_links: bool,
                 on_page: Callable[[int, int], None] | None = None, scale: float = 2.5) -> tuple[str, dict]:
    """Convert one page slice with the PaddleOCR-VL worker; on_page(n, total) is called as each page finishes."""
    stats = new_stats()
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.Popen(worker_command(pdf, page_range, server, Path(tmp), scale), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        bad: list[int] = []
        for line in proc.stdout:
            m = re.match(r"PAGE (\d+)/(\d+)", line)
            if m and on_page:
                on_page(int(m.group(1)), int(m.group(2)))
            m = re.match(r"ERROR page (\d+)", line)
            if m:
                bad.append(int(m.group(1)))
        rc = proc.wait()
        dirs = sorted(Path(tmp).glob("page_*"))
        if rc != 0 and not dirs:
            raise RuntimeError(f"the PaddleOCR-VL worker failed (exit {rc})")
        parts = []
        for d in dirs:
            page_no = int(d.name[5:])
            texts = [f.read_text(encoding="utf-8") for f in sorted(d.glob("*.md"))]
            if texts:
                link = link_target(pdf.name, page_no) if page_links else None
                parts.append(f"<!-- pdf2md:page={page_no} -->\n\n" +
                             clean_paddle_markdown("\n\n".join(texts), page_no, d, assets_dir, link))
            safety_file = d / 'safety.json'
            if safety_file.is_file():
                for report in json.loads(safety_file.read_text()):
                    stats['eq_repaired'] += len(report['formula_retries'])
                    stats['eq_pictured'] += len(report['formula_images'])
                    stats['prose_restored'] = stats.get('prose_restored',0) + len(report['prose_restored'])
                    stats['composite_figures'] = stats.get('composite_figures',0) + len(report['composite_figures'])
                    stats['inline_source_restored'] += len(report.get('inline_restored',[]))
                    stats['inline_source_images'] += len(report.get('inline_images',[]))
                    stats['running_headers'] += len(report.get('running_headers',[]))
        parts += [f"<!-- page {n} could not be read -->" for n in bad]
        stats["eq_total"] = sum(len(re.findall(r"\$\$.+?\$\$", t, re.S)) for t in parts)
        stats['eq_total'] += stats['eq_pictured']
        return "\n\n".join(parts), stats


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------
def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    n = 2
    while (q := p.with_name(f"{p.stem} ({n}){p.suffix}")).exists():
        n += 1
    return q


def find_vault_root(p: Path) -> Path | None:
    """The nearest ancestor folder that holds an `.obsidian` directory, if any."""
    for d in [p.resolve(), *p.resolve().parents]:
        if (d / ".obsidian").is_dir():
            return d
    return None


def run_backup(command: str) -> None:
    """Run the user's backup command; a failure aborts before anything is written."""
    r = subprocess.run(shlex.split(command), capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"error: backup command failed (exit {r.returncode}), nothing written:\n{r.stdout}{r.stderr}")
    shown = ""
    try:
        shown = json.loads(r.stdout).get("archive", "")
    except (ValueError, AttributeError):
        shown = next((l for l in reversed(r.stdout.splitlines()) if l.strip()), "")
    print(f"backup: {shown or 'done'}")


def convert_pdf(a: argparse.Namespace, pdf: Path) -> None:
    """Convert one PDF according to the parsed arguments."""

    vmodel = a.vision_model or a.model
    paddle_engine = a.engine == "paddleocr-vl"
    clean_mode = a.clean or ("none" if paddle_engine else "auto")
    batch_pages = a.pages_per_batch if a.pages_per_batch is not None else (PADDLE_PAGES_PER_BATCH if paddle_engine else PAGES_PER_BATCH)
    use_vision = not (a.no_llm or a.no_vision or paddle_engine)   # PaddleOCR-VL does its own reading
    need_text = not a.no_llm and clean_mode != "none"       # the text model only does prose cleanup
    text_be = Backend(a.api, a.host, a.model, a.api_key)
    vis_be = Backend(a.vision_api or a.api, a.vision_host or a.host, vmodel, a.api_key)
    if need_text:
        text_be.check()
    if use_vision:
        vis_be.check()
    page_links = a.page_links == "on" or (a.page_links == "auto" and find_vault_root(pdf) is not None)
    flag_doubt = not a.no_equation_flags
    engines = (["paddleocr-vl-1.6"] if paddle_engine else ["docling"]) + ([f"vision:{vis_be.describe()}"] if use_vision else []) \
        + ([f"text:{text_be.describe()}"] if need_text else [])

    out = (a.out.expanduser().resolve() if a.out else pdf.parent)
    if out.suffix.lower() != ".md":
        out = out / f"{pdf.stem}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out = unique_path(out)
    assets = out.parent / f"{out.stem}_assets"

    def vis(system, user, cap):
        return lambda png: vis_be.chat(system, user, a.timeout, [png], a.num_ctx, MAX_OUT[cap])

    ask_latex = vis(LATEX_SYSTEM, "Transcribe this equation.", "latex") if use_vision else None
    ask_fig = vis(FIGURE_SYSTEM, "Describe this figure.", "figure") if use_vision and not a.no_figure_descriptions else None
    ask_inline = ((lambda png, text: vis_be.chat(INLINE_SYSTEM, f"Extracted text:\n{text}", a.timeout, [png],
                                                 a.num_ctx, MAX_OUT["inline"]))
                  if use_vision and a.inline_math != "off" else None)
    ask_retry = ((lambda png, text: vis_be.chat(INLINE_STRICT_SYSTEM, f"Extracted text:\n{text}", a.timeout, [png],
                                                a.num_ctx, MAX_OUT["inline"]))
                 if ask_inline else None)
    chat = lambda masked: text_be.chat(CLEAN_SYSTEM, masked, a.timeout, None, a.num_ctx, MAX_OUT["clean"])

    total = page_count(pdf)
    first, last = parse_pages(a.pages, total)
    key = run_key(pdf, not a.no_ocr, use_vision, vmodel, a.model, bool(ask_fig), bool(ask_inline),
                  batch_pages, a.num_ctx, a.no_llm, a.inline_math, first, last, a.engine, clean_mode,
                  a.api, a.vision_api, a.vision_host, page_links, flag_doubt, need_text, a.paddle_scale)
    cache_dir = Path(tempfile.mkdtemp(prefix="pdf2md-")) if a.no_cache else CACHE_ROOT / key
    cache = Cache(cache_dir)

    t0 = time.time()
    ranges = page_ranges(last, batch_pages, first)
    pool = ThreadPoolExecutor(max_workers=max(1, a.jobs)) if (need_text or use_vision) else None
    resumed = len(list(cache_dir.glob("slice_*.json")))
    print(f"[1/4] {'PaddleOCR-VL' if paddle_engine else 'Docling'}: {pdf.name} ({(last - first + 1) if last else '?'} of {total or '?'} pages, {len(ranges)} slice(s)"
          + (f", {a.jobs} parallel jobs; {' + '.join(engines)}" if pool else "") + ")"
          + (f"; resuming, {resumed} slice(s) already done" if resumed else ""))

    conv = None
    totals = new_stats()
    failed_pages: list[int] = []
    plain: list[str] = []
    jobs: list = []                              # document order: str (already final) or Future
    llm_jobs = [0, 0]                            # submitted, finished
    skipped = [0]

    def on_done(_f):
        llm_jobs[1] += 1
        if llm_jobs[1] % 10 == 0 or llm_jobs[1] == llm_jobs[0]:
            print(f"  cleaned {llm_jobs[1]}/{llm_jobs[0]} chunk(s) submitted", flush=True)

    total_pages = (last - first + 1) if last else 0

    def progress(done: int) -> None:
        """A machine-readable line for the plugin's progress window: pages finished of pages to convert."""
        if total_pages:
            print(f"PROGRESS {max(0, min(done, total_pages))} {total_pages}", flush=True)

    guard = ServerGuard(a.paddle_server, a.serve_command, a.serve_wait) if paddle_engine else None
    if guard:
        guard.__enter__()
    progress(0)
    try:
        for n, rng in enumerate(ranges, 1):
            ck = f"slice_{rng[0]}_{rng[1]}"
            hit = cache.get(ck)
            if hit:
                md, st = hit["md"], hit["stats"]
                failed_pages += hit["failed"]
            else:
                failed_here: list[int] = []
                if paddle_engine:
                    convert_fn = lambda r: paddle_slice(pdf, r, cache.img, a.paddle_server, page_links,
                                                        lambda i, _t, r=r: progress(r[0] - first + i), a.paddle_scale)
                else:
                    if conv is None:
                        conv = make_converter(not a.no_ocr, use_vision)
                    convert_fn = lambda r: convert_slice(conv, pdf, r, cache.img, pool, ask_latex, ask_fig, ask_inline,
                                                         a.inline_math, ask_retry, page_links, flag_doubt)
                md, st = convert_resilient(convert_fn, rng, failed_here, None if paddle_engine else free_gpu_memory)
                failed_pages += failed_here
                cache.put(ck, {"md": md, "stats": st, "failed": failed_here})
            totals = merge_stats(totals, st)
            progress(rng[1] - first + 1)
            md = unescape_entities(normalize_math(md))
            el = time.time() - t0
            eta = f", ETA ~{el / n * (len(ranges) - n) / 60:.0f} min" if len(ranges) > 1 and n < len(ranges) else ""
            label = f"slice {n}/{len(ranges)}" + (f" (pages {rng[0]}-{rng[1]})" if len(ranges) > 1 else "")
            print(f"  {label}: {st['eq_total']} equation(s), {st['inline_rewritten']}/{st['inline_total']} inline "
                  f"paragraph(s) as LaTeX, {st['figs_described']} figure(s) [{el:.0f}s{eta}]", flush=True)
            for c in split_chunks(md):
                if pool and chunk_needs_cleaning(c, clean_mode):
                    f = pool.submit(cached_clean, cache, c, chat, a.model)
                    llm_jobs[0] += 1
                    jobs.append(f)
                    f.add_done_callback(on_done)
                else:
                    skipped[0] += bool(pool)
                    jobs.append(c)

        print(f"      equations: {totals['eq_total']} (tidied {totals['eq_tidied']}, re-read {totals['eq_repaired']}, "
              f"read as text {totals['eq_as_text']}, kept as picture {totals['eq_pictured']}, "
              f"flagged for checking {totals['eq_flagged']}, "
              f"unresolved {totals['eq_unresolved']}); inline paragraphs as LaTeX: {totals['inline_rewritten']}"
              f"/{totals['inline_total']} ({totals['inline_retried_ok']} on the stricter retry); "
              f"figures described: {totals['figs_described']}")
        if paddle_engine:
            print(f"      source guards: {totals['prose_restored']} prose block(s) restored, "
                  f"{totals['composite_figures']} composite figure(s) preserved, "
                  f"{totals['inline_source_restored']} inline paragraph(s) restored, "
                  f"{totals['inline_source_images']} inline paragraph(s) kept as source images, "
                  f"{totals['running_headers']} repeated running header(s) removed")
        rej = {k[4:]: v for k, v in totals.items() if k.startswith("rej:")}
        if rej:
            print("      inline paragraphs kept as plain text: " + ", ".join(f"{v} {k}" for k, v in sorted(rej.items(), key=lambda kv: -kv[1])))
        if pool:
            print(f"[2/4] {text_be.describe() if need_text else 'no text model'}: {llm_jobs[0]} chunk(s) to clean ({skipped[0]} left as-is: no damage found), "
                  f"{llm_jobs[0] - llm_jobs[1]} still running")
        done, kept = [], 0
        for i, j in enumerate(jobs, 1):
            if isinstance(j, str):
                done.append(j)
                continue
            res, bad, log = j.result()
            done.append(res)
            kept += bad
            for line in log:
                print(f"  chunk {i}: {line}")
        md = normalize_math("\n\n".join(d.strip("\n") for d in done))
        if kept:
            print(f"warning: {kept} piece(s) left uncleaned")
    finally:
        if pool:
            pool.shutdown(wait=True, cancel_futures=True)
        if guard:
            guard.__exit__()

    md = fix_headings(md)
    try:
        outline = read_pdf_outline(pdf)
        md, heading_report = repair_pdf_headings(md, outline)
        if outline:
            print(f"      PDF outline: {heading_report['matched']}/{len(outline)} headings matched; "
                  f"{len(heading_report['changes'])} repaired, {len(heading_report['ambiguous'])} ambiguous, "
                  f"{len(heading_report['unmatched'])} unmatched (left unchanged)")
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        print(f"warning: PDF outline unavailable; using extracted headings ({type(exc).__name__})")
    if failed_pages:
        print(f"warning: {len(failed_pages)} page(s) could not be converted: {sorted(failed_pages)}")
    issues = validate_document(md)
    print(f"[3/4] math check: {len(issues)} problem(s)")
    for ln, why, snip in issues[:20]:
        print(f"  body line {ln}: {why}: {snip}")
    if len(issues) > 20:
        print(f"  ... {len(issues) - 20} more")

    date = dt.date.today().isoformat()
    notes = split_book(md, pdf.stem) if a.split_chapters else None
    if a.split_chapters and not notes:
        print("warning: no numbered chapter headings found; writing a single note instead")
    folder = unique_dir(out.with_suffix("")) if notes else None
    if folder:
        assets = folder / "assets"

    if any(cache.img.iterdir()):  # figures: copy missing files, never overwrite
        assets.mkdir(parents=True, exist_ok=True)
        for f in cache.img.iterdir():
            if not (assets / f.name).exists():
                shutil.copy2(f, assets / f.name)
    if a.backup_command and not a.no_backup and find_vault_root(out.parent):
        run_backup(a.backup_command)

    if notes:
        folder.mkdir(parents=True, exist_ok=True)
        index_stem = "00 Index"
        files = [n["file"] for n in notes]
        for i, n in enumerate(notes):
            text = render_note(n, pdf.stem, pdf.name, files[i - 1] if i else None,
                               files[i + 1] if i + 1 < len(files) else None, index_stem, date,
                               len(validate_document(n["body"])))
            with open(folder / f"{n['file']}.md", "x", encoding="utf-8") as f:
                f.write(text)
        with open(folder / f"{index_stem}.md", "x", encoding="utf-8") as f:
            f.write(render_index(notes, pdf.stem, pdf.name, date))
        print(f"[4/4] wrote {len(notes)} notes + index to {folder} ({time.time() - t0:.0f}s)")
        for n in notes:
            print(f"      {n['file']}  ({n['body'].count(chr(10)) + 1} lines)")
    else:
        front = (f"---\ntitle: \"{pdf.stem}\"\nsource_pdf: \"{pdf.name}\"\n"
                 f"converted: {date}\n"
                 f"converter: {' + '.join(engines)}\n"
                 f"math_warnings: {len(issues)}\n"
                 + (f"pages: \"{first}-{last}\"\n" if a.pages else "")
                 + (f"pages_failed: {sorted(failed_pages)}\n" if failed_pages else "")
                 + "tags: [pdf_import]\n---\n\n")
        with open(out, "x", encoding="utf-8") as f:  # exclusive: never overwrite
            f.write(front + md.strip() + "\n")
        print(f"[4/4] wrote {out} ({time.time() - t0:.0f}s)")
    if assets.exists():
        print(f"      figures: {assets}")
    if not (a.keep_cache and not a.no_cache):
        shutil.rmtree(cache_dir, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="PDF -> Obsidian Markdown (Docling + a local vision model)")
    ap.add_argument("pdf", type=Path, nargs="+", help="one or more PDFs, converted one after another")
    ap.add_argument("-o", "--out", type=Path, help="output .md path or directory (default: next to the PDF)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama text model (default {DEFAULT_MODEL})")
    ap.add_argument("--vision-model", help="Ollama vision model for equations/figures (default: --model)")
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help="address of the model server (Ollama, or an OpenAI-compatible server such as llama.cpp)")
    ap.add_argument("--api", choices=API_CHOICES, default="ollama",
                    help="ollama (default) or openai = any OpenAI-compatible server, e.g. llama-server")
    ap.add_argument("--api-key", default=os.environ.get("PDF2MD_API_KEY"),
                    help="API key for the server, if it needs one (default: $PDF2MD_API_KEY)")
    ap.add_argument("--vision-api", choices=API_CHOICES, help="API of the vision server (default: --api)")
    ap.add_argument("--vision-host", help="address of the vision server (default: --host)")
    ap.add_argument("--page-links", choices=("auto", "on", "off"), default="auto",
                    help="add a link to the PDF page under each heading; auto = when the PDF is inside an Obsidian vault")
    ap.add_argument("--no-equation-flags", action="store_true",
                    help="do not add 'check this equation' callouts for equations that look cut off")
    ap.add_argument("--jobs", type=int, default=DEFAULT_JOBS,
                    help=f"parallel Ollama requests (default {DEFAULT_JOBS}; match OLLAMA_NUM_PARALLEL)")
    ap.add_argument("--engine", choices=ENGINES, default="docling",
                    help="docling = Docling layout + a vision model repairing equations (default); paddleocr-vl = "
                         "PaddleOCR-VL-1.6 served by llama.cpp (needs --paddle-server; no Ollama)")
    ap.add_argument("--paddle-server", default=os.environ.get("PDF2MD_PADDLE_SERVER", DEFAULT_PADDLE_SERVER),
                    help="address of the llama-server running PaddleOCR-VL (default: $PDF2MD_PADDLE_SERVER, "
                         f"{DEFAULT_PADDLE_SERVER})")
    ap.add_argument("--paddle-scale", type=float, default=float(os.environ.get("PDF2MD_PADDLE_SCALE", "2.5")),
                    help="page render scale: 2.5 (180 dpi, balanced default), 4 (288 dpi, table-heavy PDFs)")
    ap.add_argument("--serve-command", default=os.environ.get("PDF2MD_SERVE_COMMAND"),
                    help="command that starts that server; run only if nothing answers, stopped when done "
                         "(default: $PDF2MD_SERVE_COMMAND; see serve-paddleocr-vl.sh)")
    ap.add_argument("--serve-wait", type=int, default=180, help="seconds to wait for --serve-command's server")
    ap.add_argument("--pages-per-batch", type=int, default=None,
                    help=f"convert big PDFs in slices of N pages (default {PAGES_PER_BATCH}, or "
                         f"{PADDLE_PAGES_PER_BATCH} with paddleocr-vl; 0 = whole file)")
    ap.add_argument("--pages", help="convert only these pages, e.g. 10-40 (1-based, inclusive)")
    ap.add_argument("--split-chapters", action="store_true",
                    help="write a folder of notes (contents, front matter, one note per chapter, back matter) "
                         "instead of one big note; needs numbered chapter headings")
    ap.add_argument("--clean", choices=CLEAN_MODES, default=None,
                    help="LLM prose cleanup: auto = only chunks showing extraction damage (default for docling), "
                         "all, none (default for paddleocr-vl)")
    ap.add_argument("--num-ctx", type=int, default=NUM_CTX, help="Ollama context size per request")
    ap.add_argument("--no-llm", action="store_true", help="skip every Ollama step")
    ap.add_argument("--no-vision", action="store_true", help="skip equation re-reading, inline math and figure descriptions")
    ap.add_argument("--inline-math", choices=INLINE_MODES, default="auto",
                    help="rewrite paragraphs with inline maths as LaTeX: auto = math-looking ones (default), "
                         "all = every paragraph (slow, best for maths books), off")
    ap.add_argument("--no-figure-descriptions", action="store_true", help="do not caption figures")
    ap.add_argument("--no-ocr", action="store_true", help="disable OCR (text PDFs only; faster)")
    ap.add_argument("--backup-command", default=os.environ.get("PDF2MD_BACKUP_COMMAND"),
                    help="command to run before writing into an Obsidian vault (default: $PDF2MD_BACKUP_COMMAND, none)")
    ap.add_argument("--no-backup", action="store_true", help="skip the backup command")
    ap.add_argument("--no-cache", action="store_true", help="no checkpointing (an interrupted run cannot resume)")
    ap.add_argument("--keep-cache", action="store_true", help="keep the checkpoint folder after success")
    ap.add_argument("--timeout", type=int, default=600, help="seconds per Ollama call")
    a = ap.parse_args()
    if not 1 <= a.paddle_scale <= 4:
        ap.error("--paddle-scale must be between 1 and 4")

    pdfs = [p.expanduser().resolve() for p in a.pdf]
    bad = [str(p) for p in pdfs if not p.is_file() or p.suffix.lower() != ".pdf"]
    if bad:
        sys.exit("error: not a PDF file: " + ", ".join(bad))
    if len(pdfs) > 1 and a.out and a.out.suffix.lower() == ".md":
        sys.exit("error: with several PDFs, -o must be a folder, not a .md file")
    failures: list[str] = []
    for i, pdf in enumerate(pdfs, 1):
        if len(pdfs) > 1:
            print(f"\n=== PDF {i}/{len(pdfs)}: {pdf.name}", flush=True)
        try:
            convert_pdf(a, pdf)
        except SystemExit as e:       # one bad PDF must not stop the rest of a batch
            if len(pdfs) == 1:
                raise
            print(f"error: {pdf.name}: {e.code}", file=sys.stderr, flush=True)
            failures.append(pdf.name)
    if failures:
        sys.exit(f"error: {len(failures)} of {len(pdfs)} PDF(s) failed: " + "; ".join(failures))


if __name__ == "__main__":
    main()
