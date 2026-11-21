"""Anchors on equations, captions and theorem-like labels, and links from prose references to them.

Obsidian flavour (plain=False), per https://obsidian.md/help/links ("Link to a block in a note"):
  * a simple paragraph carries its block id at the end of its last line: `text ^id`;
  * structured blocks (callouts/quotes, tables, lists) and display math take the id on its own line with a blank line
    before and after: `$$...$$`, blank, `^id`, blank. Links to parts *inside* a callout are not supported, so an
    equation inside a callout is anchored on the callout, and only when the callout has no label id of its own.
  * links: `[[stem#^id|label]]`, same note `[[#^id|label]]`.
Plain flavour (plain=True): `<a id="id"></a>` on its own line (blank-line separated) before the block (inside the
quote for equations in a blockquote); links `[label](<quoted stem>.md#id)` / `[label](#id)`.
"""
from __future__ import annotations

import re
import urllib.parse

from pdf2md_local.structure import CALLOUT_TYPES, parse_label, split_blocks

NUM = r"(?:[A-Z]\.\d+(?:\.\d+)*|\d+(?:\.\d+)*)[a-z]?"
PREFIX = {"Theorem": "thm", "Lemma": "lem", "Proposition": "prop", "Corollary": "cor", "Definition": "def",
          "Example": "ex", "Remark": "rem", "Exercise": "exr"}
_ENV = "|".join(PREFIX)
_QUOTE_PREFIX = re.compile(r"^[ \t]*(?:>[ \t]?)+")
CAPTION = re.compile(
    r"^(?P<open>\*\*\*|\*\*|\*|_)?(?P<word>Figure|Fig\.|Table)[ \t]+(?P<num>" + NUM + r")(?![\d]|\.\d)(?P<cl>\*\*\*|\*\*|\*|_)?"
    r"(?:[ \t]*[.:|—–-](?!\d)(?:\*\*\*|\*\*|\*|_)?|[ \t]+(?=[A-Z]))")
_CALLOUT_HEAD = re.compile(r"^\[!(?P<type>[A-Za-z-]+)\][+-]?[ \t]*(?P<word>" + _ENV + r")[ \t]+(?P<num>" + NUM + r")(?!\w)")
_TAG = re.compile(r"\\tag\*?\{\s*\(?\s*(" + NUM + r")\s*\)?\s*\}")
_TRAIL = re.compile(r"(?:\\q?quad|\\hfill|\\;|\\eqno|~|&)[ \t]*\(\s*(" + NUM + r")\s*\)[ \t]*$")
_TRAIL_TEXT = re.compile(r"(?:\\q?quad|\\hfill|\\;|~|&)[ \t]*\\text\{\s*\(\s*(" + NUM + r")\s*\)\s*\}[ \t]*$")
_EXIST = re.compile(r"(?m)(?:^|[ \t])\^([A-Za-z0-9-]+)[ \t]*$|<a id=\"([^\"]+)\"></a>")
_OWN_ID = re.compile(r"(?:^|[ \t])\^[A-Za-z0-9-]+[ \t]*$")


def norm(num: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", num).strip("-")


def _unquote(line: str) -> str:
    return _QUOTE_PREFIX.sub("", line)


def _math_number(inner: list[str]) -> str | None:
    text = "\n".join(inner).strip()
    if text.startswith("$$"):
        text = text[2:]
    if text.endswith("$$"):
        text = text[:-2]
    text = text.strip()
    tags = _TAG.findall(text)
    if len(tags) == 1:
        return tags[0]
    if tags:
        return None
    m = _TRAIL.search(text) or _TRAIL_TEXT.search(text)
    return m[1] if m else None


def scan_targets(lines: list[str]) -> list[dict]:
    """Candidate anchor targets in document order. Each: id, mode (math|para|quote|inmath), a, b (block lines,
    end exclusive) and for inmath also qa, qb (the enclosing quote block)."""
    out: list[dict] = []
    for a, b, kind in split_blocks(lines):
        if kind == "math":
            n = _math_number(lines[a:b])
            if n:
                out.append({"id": "eq-" + norm(n), "mode": "math", "a": a, "b": b})
        elif kind == "para":
            first = lines[a]
            m = CAPTION.match(first)
            if m:
                pre = "fig" if m["word"].startswith("Fig") else "tab"
                out.append({"id": f"{pre}-{norm(m['num'])}", "mode": "para", "a": a, "b": b})
                continue
            m = parse_label(first)
            if m and m["num"] and m["word"] in PREFIX:
                out.append({"id": f"{PREFIX[m['word']]}-{norm(m['num'])}", "mode": "para", "a": a, "b": b})
        elif kind == "quote":
            inner = [_unquote(l) for l in lines[a:b]]
            h = _CALLOUT_HEAD.match(inner[0].strip())
            m = None
            if h and h["type"].lower() in CALLOUT_TYPES.values():
                word, num = h["word"], h["num"]
            else:
                m = parse_label(inner[0].strip())
                word, num = (m["word"], m["num"]) if m else (None, None)
            if word in PREFIX and num:
                out.append({"id": f"{PREFIX[word]}-{norm(num)}", "mode": "quote", "a": a, "b": b})
            for ia, ib, ik in split_blocks(inner):
                if ik == "math":
                    n = _math_number(inner[ia:ib])
                    if n:
                        out.append({"id": "eq-" + norm(n), "mode": "inmath", "a": a + ia, "b": a + ib, "qa": a, "qb": b})
    return out


def _has_own_id(lines: list[str], t: dict) -> bool:
    if t["mode"] == "para":
        return bool(_OWN_ID.search(lines[t["b"] - 1]))
    end = t["qb"] if t["mode"] == "inmath" else t["b"]
    j = end
    while j < len(lines) and not lines[j].strip():
        j += 1
    return j < len(lines) and re.fullmatch(r"\^[A-Za-z0-9-]+", lines[j].strip()) is not None and j - end <= 2


def _anchor_edits(lines: list[str], t: dict, plain: bool) -> tuple[int, int, list[str]]:
    """-> (kind, position, new lines): kind 0 = append suffix to line `position` (list has the suffix),
    1 = insert before line `position`, 2 = insert after the block ending at `position` (exclusive end)."""
    i = t["id"]
    if plain:
        a = t["a"]
        pre = ""
        if t["mode"] == "inmath":
            pre = _QUOTE_PREFIX.match(lines[a])[0]
        out = [f'{pre}<a id="{i}"></a>', pre.rstrip()]
        if a > 0 and _unquote(lines[a - 1]).strip():
            out.insert(0, pre.rstrip())
        return 1, a, out
    if t["mode"] == "para":
        return 0, t["b"] - 1, [f" ^{i}"]
    end = t["qb"] if t["mode"] == "inmath" else t["b"]
    out = ["", f"^{i}"]
    if end < len(lines) and lines[end].strip():
        out.append("")
    return 2, end, out


def _protect(text: str, extra_first: tuple[int, int] | None = None, urls: bool = True) -> str:
    """Replace protected regions (code, math, links, html, urls, headings, tables, callout headers) with NULs."""
    t = text
    if extra_first:
        t = t[:extra_first[0]] + "\0" * (extra_first[1] - extra_first[0]) + t[extra_first[1]:]
    for rx in _MASKS:
        if rx is _URL_MASK and not urls:
            continue
        t = rx.sub(lambda m: "\0" * len(m[0]), t)
    return t


_URL_MASK = re.compile(r"(?:https?|ftp)://[^\s)>\]]+|<https?://[^>]+>")
_MASKS = [
    re.compile(r"(`+)(?:(?!\1).)+?\1", re.S),
    re.compile(r"\$\$.*?\$\$", re.S),
    re.compile(r"(?<![\\$])\$(?!\$)(?:\\.|[^$\\])+?\$(?!\$)", re.S),
    re.compile(r"!?\[\[.*?\]\]", re.S),
    re.compile(r"!?\[[^\]\n]*\]\([^)\n]*\)"),
    re.compile(r"<a [^>]*>.*?</a>|<[A-Za-z!/][^>\n]*>|<!--.*?-->", re.S),
    _URL_MASK,
    re.compile(r"(?m)^[ \t>]*\[![A-Za-z-]+\].*$"),
    re.compile(r"(?m)^[ \t>]*(?:#{1,6}[ \t].*|\|.*)$"),
    re.compile(r"(?:^|(?<=[ \t]))\^[A-Za-z0-9-]+[ \t]*$", re.M),
]

_EQ = re.compile(r"(?<![\w.])(?P<w>Eqs?\.?|eqs?\.?|Equations?|equations?)[ \t~]*\((?P<n>" + NUM + r")\)")
_EQ_CHAIN = re.compile(r"[ \t]*(?:,[ \t]*(?:and[ \t]+|or[ \t]+)?|(?:and|or|&)[ \t]+)\((?P<n>" + NUM + r")\)")
_REF = re.compile(
    r"(?<![\w.])(?P<w>(?P<fig>Figs?\.?|Figures?)|(?P<tab>Tables?)|(?P<env>(?:" + _ENV + r")s?))[ \t~]+(?P<n>" + NUM + r")(?!\w|\.\d)")
_BARE_EQ = re.compile(r"(?<![\w.)\]])\((?P<n>(?:[A-Z]\.\d+|\d+\.\d+)(?:\.\d+)*[a-z]?)\)")
SECNUM = r"(?:\d+(?:\.\d+)*|[A-Z](?:\.\d+)+)"
_SEC = re.compile(r"(?<![\w.])(?P<w>(?P<sec>Sections?|Secs?\.)|(?P<chap>Chapters?))[ \t~]+(?P<n>" + SECNUM + r")(?!\w|\.\d)")
_SEC_CHAIN = re.compile(r"[ \t]*(?:,[ \t]*(?:and[ \t]+)?|and[ \t]+)(?P<n>" + SECNUM + r")(?!\w|\.\d)")
_HEAD_NUM = re.compile(r"^(" + SECNUM + r")\.?(?=\s|$)")
_HEAD = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$")
_NO_LINK = re.compile(r"[#|^\[\]$`*_<>]")
_NUM_CHAIN = re.compile(r"[ \t]*(?:,[ \t]*(?:and[ \t]+)?|and[ \t]+)(?P<n>" + NUM + r")(?!\w)")


def _refs(masked: str):
    """Yield (start, end, id-candidates) for every reference."""
    for m in _EQ.finditer(masked):
        yield m.start(), m.end(), "eq", m["n"]
        pos = m.end()
        while True:
            c = _EQ_CHAIN.match(masked, pos)
            if not c:
                break
            yield c.start("n") - 1, c.end(), "eq", c["n"]
            pos = c.end()
    for m in _BARE_EQ.finditer(masked):
        yield m.start(), m.end(), "eq", m["n"]
    for m in _SEC.finditer(masked):
        pre = "sec" if m["sec"] else "chap"
        yield m.start(), m.end(), pre, m["n"]
        if m["w"].rstrip(".").endswith("s") and m["sec"] and not m["w"].startswith("Sec."):
            pos = m.end()
            while True:
                c = _SEC_CHAIN.match(masked, pos)
                if not c:
                    break
                yield c.start("n"), c.end(), pre, c["n"]
                pos = c.end()
    for m in _REF.finditer(masked):
        if m["fig"]:
            pre = "fig"
        elif m["tab"]:
            pre = "tab"
        else:
            pre = PREFIX[m["env"].rstrip("s")]
        yield m.start(), m.end(), pre, m["n"]
        plural = m["w"].rstrip(".").endswith("s")
        if plural:
            pos = m.end()
            while True:
                c = _NUM_CHAIN.match(masked, pos)
                if not c:
                    break
                yield c.start("n"), c.end(), pre, c["n"]
                pos = c.end()


def _resolve(registry: dict, pre: str, num: str):
    i = f"{pre}-{norm(num)}"
    if i in registry:
        return i
    if num[-1:].islower():
        i = f"{pre}-{norm(num[:-1])}"
        if i in registry:
            return i
    return None


def add_cross_links(notes: list[dict], plain: bool) -> int:
    """Anchor targets and link references across `notes` (dicts with `file` stem and `body`), mutating bodies.
    Returns the number of links made. Idempotent."""
    return link_references(notes, add_anchors(notes, plain), plain)


def add_anchors(notes: list[dict], plain: bool) -> dict[str, str]:
    """Anchor the targets (equations, captions, labelled environments); returns {anchor id: note stem}."""
    registry: dict[str, str] = {}
    for n in notes:  # ids already present (earlier run): first occurrence in the book wins, never duplicated
        for m in _EXIST.finditer(n["body"]):
            registry.setdefault(m[1] or m[2], n["file"])
    edits: dict[int, list[tuple[int, int, list[str]]]] = {}
    for ni, n in enumerate(notes):
        lines = n["body"].split("\n")
        taken: set[int] = set()
        for t in scan_targets(lines):
            if t["id"] in registry:
                continue
            if t["mode"] == "inmath" and not plain:
                if t["qa"] in taken or _has_own_id(lines, t):
                    continue
                taken.add(t["qa"])
            elif t["mode"] == "quote" and not plain:
                if t["a"] in taken or _has_own_id(lines, t):
                    continue
                taken.add(t["a"])
            elif not plain and _has_own_id(lines, t):
                continue
            registry[t["id"]] = n["file"]
            edits.setdefault(ni, []).append(_anchor_edits(lines, t, plain))
        if ni in edits:
            for kind, pos, new in sorted(edits[ni], key=lambda e: -e[1]):
                if kind == 0:
                    lines[pos] += new[0]
                elif kind == 1:
                    lines[pos:pos] = new
                else:
                    lines[pos:pos] = new
            n["body"] = "\n".join(lines)
    return registry


def link_references(notes: list[dict], registry: dict[str, str], plain: bool) -> int:
    """Link prose references to the anchors in `registry` (and, in the obsidian flavour, section references)."""
    made = 0
    headings = {} if plain else _heading_index(notes)
    for n in notes:
        made += _link_note(n, registry, plain, headings)
    return made


def _heading_index(notes: list[dict]) -> dict:
    """{("sec"|"chap", number): [(note file, heading text), ...]} for headings that start with a section number.
    Split notes carry their chapter heading outside `body` (rendered later as `# <chapter> <title>`)."""
    idx: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for n in notes:
        if n.get("chapter") and n.get("title"):
            idx.setdefault(("chap", str(n["chapter"])), []).append((n["file"], f"{n['chapter']} {n['title']}"))
            idx.setdefault(("sec", str(n["chapter"])), []).append((n["file"], f"{n['chapter']} {n['title']}"))
        lines = n["body"].split("\n")
        for a, b, kind in split_blocks(lines):
            if kind != "heading":
                continue
            m = _HEAD.match(lines[a].strip())
            if not m:
                continue
            text = m[1].strip()
            h = _HEAD_NUM.match(text)
            if not h:
                continue
            level = len(lines[a].strip()) - len(lines[a].strip().lstrip("#"))
            idx.setdefault(("sec", h[1]), []).append((n["file"], text))
            if level == 1:
                idx.setdefault(("chap", h[1]), []).append((n["file"], text))
    return idx


def _link_note(note: dict, registry: dict, plain: bool, headings: dict) -> int:
    lines = note["body"].split("\n")
    own: list[tuple[int, int, str]] = [(t["a"] if t["mode"] != "inmath" else t["qa"],
                                        t["b"] if t["mode"] != "inmath" else t["qb"], t["id"])
                                       for t in scan_targets(lines)]
    out: list[str] = []
    made = 0
    for a, b, kind in split_blocks(lines):
        if kind not in ("para", "quote", "list"):
            out.extend(lines[a:b])
            continue
        text = "\n".join(lines[a:b])
        first = lines[a]
        pm = _QUOTE_PREFIX.match(first)
        p = pm.end() if pm else 0
        span = None
        head = first[p:]
        lm = CAPTION.match(head) or parse_label(head)
        if lm:
            span = (p, p + lm.end())
        masked = _protect(text, span)
        skip = {i for (oa, ob, i) in own if oa >= a and ob <= b}
        reps = []
        for s, e, pre, num in _refs(masked):
            if pre in ("sec", "chap"):
                hits = headings.get((pre, num), [])
                if len(hits) != 1 or _NO_LINK.search(hits[0][1]):
                    continue
                dest, htext = hits[0]
                label = text[s:e]
                reps.append((s, e, f"[[{'' if dest == note['file'] else dest}#{htext}|{label}]]"))
                continue
            target = _resolve(registry, pre, num)
            if not target or target in skip or "\0" in masked[s:e]:
                continue
            label = text[s:e]
            if "|" in label or "]" in label or "[" in label:
                continue
            dest = registry[target]
            if plain:
                url = "#" + target if dest == note["file"] else urllib.parse.quote(dest + ".md", safe="/#=") + "#" + target
                reps.append((s, e, f"[{label}]({url})"))
            else:
                reps.append((s, e, f"[[{'' if dest == note['file'] else dest}#^{target}|{label}]]"))
        keep, last_end = [], -1
        for s, e, rep in sorted(reps, key=lambda r: (r[0], -r[1])):
            if s >= last_end:  # overlapping matches (e.g. "Eq. (1.2)" and the bare "(1.2)" inside it): first wins
                keep.append((s, e, rep))
                last_end = e
        for s, e, rep in reversed(keep):
            text = text[:s] + rep + text[e:]
            made += 1
        out.extend(text.split("\n"))
    note["body"] = "\n".join(out)
    return made
