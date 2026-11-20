# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["docling"]
# ///
"""Convert a PDF to Obsidian-flavoured Markdown with Docling + a local Ollama model.

Pipeline
  1. Docling parses the PDF (layout, tables, OCR, formula -> LaTeX, figures).
  2. Equations are tidied and validated. Undecoded or invalid ones are re-read from a
     crop of the page by the Ollama vision model; figures get a short description.
  3. Math is normalised deterministically: inline $...$, block $$ on their own lines.
  4. Ollama (default gemma4:12b) cleans the prose chunk by chunk. Math, tables, code,
     image embeds and figure descriptions are swapped for placeholders first, so the
     model cannot alter them. A chunk whose placeholders do not come back intact is
     retried, then split in halves, and only then kept as-is.
  5. The finished note is checked for math that will not render, and written to a NEW
     file (never overwrites), figures as ![[embeds]].

Run:  pdf2md paper.pdf            (see pdf2md --help)
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
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
import urllib.error
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
    raw = "|".join(map(str, (pdf, st.st_size, st.st_mtime_ns, hashlib.sha1(src).hexdigest(), *parts)))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def new_stats() -> dict:
    return {"eq_total": 0, "eq_tidied": 0, "eq_repaired": 0, "eq_unresolved": 0, "eq_as_text": 0, "eq_pictured": 0,
            "figs_described": 0, "inline_total": 0, "inline_rewritten": 0, "inline_retried_ok": 0}


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
                  ask_retry: Callable[[bytes, str], str] | None = None) -> tuple[str, dict]:
    """Convert one page slice. Equation re-reads, figure captions and inline-math rewrites run in parallel."""
    from docling_core.types.doc import DocItemLabel, FormulaItem, ImageRefMode, PictureItem, TextItem

    doc = conv.convert(str(pdf), page_range=page_range).document
    stats = new_stats()

    # pass 1: crop images here (PIL), start every model call at once
    formulas: list[tuple] = []
    pictures: list[Future | None] = []
    inlines: list[tuple] = []
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


def main() -> None:
    ap = argparse.ArgumentParser(description="PDF -> Obsidian Markdown (Docling + Ollama)")
    ap.add_argument("pdf", type=Path)
    ap.add_argument("-o", "--out", type=Path, help="output .md path or directory (default: next to the PDF)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama text model (default {DEFAULT_MODEL})")
    ap.add_argument("--vision-model", help="Ollama vision model for equations/figures (default: --model)")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--jobs", type=int, default=DEFAULT_JOBS,
                    help=f"parallel Ollama requests (default {DEFAULT_JOBS}; match OLLAMA_NUM_PARALLEL)")
    ap.add_argument("--pages-per-batch", type=int, default=PAGES_PER_BATCH,
                    help=f"convert big PDFs in slices of N pages to bound memory (default {PAGES_PER_BATCH}, 0 = whole file)")
    ap.add_argument("--pages", help="convert only these pages, e.g. 10-40 (1-based, inclusive)")
    ap.add_argument("--split-chapters", action="store_true",
                    help="write a folder of notes (contents, front matter, one note per chapter, back matter) "
                         "instead of one big note; needs numbered chapter headings")
    ap.add_argument("--clean", choices=CLEAN_MODES, default="auto",
                    help="LLM prose cleanup: auto = only chunks showing extraction damage (default), all, none")
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

    pdf = a.pdf.expanduser().resolve()
    if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
        sys.exit(f"error: not a PDF file: {pdf}")
    vmodel = a.vision_model or a.model
    use_vision = not (a.no_llm or a.no_vision)
    if not a.no_llm:
        check_ollama(a.host, a.model)
        if use_vision and vmodel != a.model:
            check_ollama(a.host, vmodel)

    out = (a.out.expanduser().resolve() if a.out else pdf.parent)
    if out.suffix.lower() != ".md":
        out = out / f"{pdf.stem}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out = unique_path(out)
    assets = out.parent / f"{out.stem}_assets"

    def vis(system, user, cap):
        return lambda png: ollama_chat(a.host, vmodel, system, user, a.timeout, [png], a.num_ctx, MAX_OUT[cap])

    ask_latex = vis(LATEX_SYSTEM, "Transcribe this equation.", "latex") if use_vision else None
    ask_fig = vis(FIGURE_SYSTEM, "Describe this figure.", "figure") if use_vision and not a.no_figure_descriptions else None
    ask_inline = ((lambda png, text: ollama_chat(a.host, vmodel, INLINE_SYSTEM, f"Extracted text:\n{text}",
                                                 a.timeout, [png], a.num_ctx, MAX_OUT["inline"]))
                  if use_vision and a.inline_math != "off" else None)
    ask_retry = ((lambda png, text: ollama_chat(a.host, vmodel, INLINE_STRICT_SYSTEM, f"Extracted text:\n{text}",
                                                a.timeout, [png], a.num_ctx, MAX_OUT["inline"]))
                 if ask_inline else None)
    chat = lambda masked: ollama_chat(a.host, a.model, CLEAN_SYSTEM, masked, a.timeout, None, a.num_ctx, MAX_OUT["clean"])

    total = page_count(pdf)
    first, last = parse_pages(a.pages, total)
    key = run_key(pdf, not a.no_ocr, use_vision, vmodel, a.model, bool(ask_fig), bool(ask_inline),
                  a.pages_per_batch, a.num_ctx, a.no_llm, a.inline_math, first, last)
    cache_dir = Path(tempfile.mkdtemp(prefix="pdf2md-")) if a.no_cache else CACHE_ROOT / key
    cache = Cache(cache_dir)

    t0 = time.time()
    ranges = page_ranges(last, a.pages_per_batch, first)
    pool = ThreadPoolExecutor(max_workers=max(1, a.jobs)) if not a.no_llm else None
    resumed = len(list(cache_dir.glob("slice_*.json")))
    print(f"[1/4] Docling: {pdf.name} ({(last - first + 1) if last else '?'} of {total or '?'} pages, {len(ranges)} slice(s)"
          + (f", {a.jobs} parallel Ollama jobs, {a.model}" if pool else "") + ")"
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

    try:
        for n, rng in enumerate(ranges, 1):
            ck = f"slice_{rng[0]}_{rng[1]}"
            hit = cache.get(ck)
            if hit:
                md, st = hit["md"], hit["stats"]
                failed_pages += hit["failed"]
            else:
                if conv is None:
                    conv = make_converter(not a.no_ocr, use_vision)
                failed_here: list[int] = []
                md, st = convert_resilient(
                    lambda r: convert_slice(conv, pdf, r, cache.img, pool, ask_latex, ask_fig, ask_inline, a.inline_math, ask_retry),
                    rng, failed_here, free_gpu_memory)
                failed_pages += failed_here
                cache.put(ck, {"md": md, "stats": st, "failed": failed_here})
            totals = merge_stats(totals, st)
            md = unescape_entities(normalize_math(md))
            el = time.time() - t0
            eta = f", ETA ~{el / n * (len(ranges) - n) / 60:.0f} min" if len(ranges) > 1 and n < len(ranges) else ""
            label = f"slice {n}/{len(ranges)}" + (f" (pages {rng[0]}-{rng[1]})" if len(ranges) > 1 else "")
            print(f"  {label}: {st['eq_total']} equation(s), {st['inline_rewritten']}/{st['inline_total']} inline "
                  f"paragraph(s) as LaTeX, {st['figs_described']} figure(s) [{el:.0f}s{eta}]", flush=True)
            for c in split_chunks(md):
                if pool and chunk_needs_cleaning(c, a.clean):
                    f = pool.submit(cached_clean, cache, c, chat, a.model)
                    llm_jobs[0] += 1
                    jobs.append(f)
                    f.add_done_callback(on_done)
                else:
                    skipped[0] += bool(pool)
                    jobs.append(c)

        print(f"      equations: {totals['eq_total']} (tidied {totals['eq_tidied']}, re-read {totals['eq_repaired']}, "
              f"read as text {totals['eq_as_text']}, kept as picture {totals['eq_pictured']}, "
              f"unresolved {totals['eq_unresolved']}); inline paragraphs as LaTeX: {totals['inline_rewritten']}"
              f"/{totals['inline_total']} ({totals['inline_retried_ok']} on the stricter retry); "
              f"figures described: {totals['figs_described']}")
        rej = {k[4:]: v for k, v in totals.items() if k.startswith("rej:")}
        if rej:
            print("      inline paragraphs kept as plain text: " + ", ".join(f"{v} {k}" for k, v in sorted(rej.items(), key=lambda kv: -kv[1])))
        if pool:
            print(f"[2/4] {a.model}: {llm_jobs[0]} chunk(s) to clean ({skipped[0]} left as-is: no damage found), "
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

    md = fix_headings(md)
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
                 f"converter: docling{'' if a.no_llm else f' + ollama:{a.model}'}\n"
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


if __name__ == "__main__":
    main()
