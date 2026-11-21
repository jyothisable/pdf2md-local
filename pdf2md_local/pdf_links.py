"""The PDF's own link annotations (internal GoTo links and external URLs), read with pypdf, and their application
to converted Markdown. Everything degrades to "no links" on any error or when pypdf is missing.

read_links() gives, per link, the text under its rectangle (with a little context to its left/right on the same
line), and for internal links the destination page/y and the first text line at that position. apply_links() then
finds that text on the converted page (between the `<!-- pdf2md:page=N -->` markers) and links it.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
import urllib.parse
from pathlib import Path

from pdf2md_local.structure import split_blocks
from pdf2md_local.xref import NUM, PREFIX, _HEAD, _NO_LINK, _protect, norm

_SCHEMES = ("http", "https", "mailto")
_PAGE_MARK = re.compile(r"^(?:>[ \t]?)*<!-- pdf2md:page=(\d+) -->\s*$")


# ---------------------------------------------------------------------------------------------------------------
# Reading the PDF
# ---------------------------------------------------------------------------------------------------------------
def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s.replace("­", ""))).strip()


def _font_widths(font, cache: dict):
    """char -> width in 1/1000 em for a font resource (pypdf's own font model), or None."""
    if font is None:
        return None
    key = id(font)
    if key not in cache:
        try:
            from pypdf.generic._font import Font
            f = Font.from_font_resource(font)
            widths = dict(f.character_widths)
            widths.setdefault(" ", f.space_width)
            for code, uni in (f.character_map or {}).items():  # CID fonts: widths are keyed by code, text is Unicode
                if isinstance(uni, str) and len(uni) == 1 and code in f.character_widths:
                    widths[uni] = f.character_widths[code]
            cache[key] = widths
        except Exception:  # noqa: BLE001  (older pypdf, odd fonts)
            cache[key] = None
    return cache[key]


class _PageText:
    """Text of one page as positioned characters: fragments from pypdf's visitor, each laid out with the font's
    character widths (a flat 0.5 em estimate when widths are unavailable)."""

    def __init__(self, page):
        frags = []
        fonts: dict = {}

        def visit(text, cm, tm, font, size):
            if not text or not text.strip("\n"):
                return
            try:
                x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
                y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
                det = abs((tm[0] * tm[3] - tm[1] * tm[2]) * (cm[0] * cm[3] - cm[1] * cm[2]))
                sz = float(size or 10) * (det ** 0.5 if det else 1.0) or 10.0
            except Exception:  # noqa: BLE001
                return
            text = text.replace("\n", "")
            widths = _font_widths(font, fonts)
            ws = []
            for ch in text:
                w = None
                if widths:
                    w = widths.get(ch, widths.get("default"))
                ws.append((w if w else 500.0) * sz / 1000.0)
            frags.append((x, y, text, ws))

        page.extract_text(visitor_text=visit)
        order = sorted(range(len(frags)), key=lambda i: (-round(frags[i][1], 0), frags[i][0]))
        self.chars: list[tuple[float, float, str, float]] = []  # (centre x, baseline y, char, width)
        for pos, i in enumerate(order):
            x, y, text, ws = frags[i]
            total = sum(ws)
            for j in order[pos + 1:pos + 8]:
                x2, y2 = frags[j][0], frags[j][1]
                if abs(y2 - y) < 1.5 and x2 > x + 0.1:
                    if total > x2 - x:  # kerning/overlap: never run into the next fragment
                        ws = [w * (x2 - x) / total for w in ws]
                    break
            cx = x
            for ch, w in zip(text, ws):
                self.chars.append((cx + w / 2, y, ch, w))
                cx += w

    @staticmethod
    def _tokens(chars) -> list[list[tuple[float, str, float]]]:
        """Glyphs of one line grouped into words (split at spaces and at visible gaps)."""
        toks, cur, prev_right = [], [], None
        for x, ch, w in sorted(chars):
            gap = prev_right is not None and x - w / 2 - prev_right > 1.2
            if ch.isspace() or gap:
                if cur:
                    toks.append(cur)
                cur = []
            if not ch.isspace():
                cur.append((x, ch, w))
            prev_right = x + w / 2
        if cur:
            toks.append(cur)
        return toks

    @classmethod
    def _join(cls, chars) -> str:
        return " ".join("".join(c[1] for c in t) for t in cls._tokens(chars))

    def line_at(self, y: float, tol: float = 2.5):
        return [(x, ch, w) for x, cy, ch, w in self.chars if abs(cy - y) <= tol]

    def in_rect(self, rect: tuple[float, float, float, float], pad: float = 1.0):
        """(text, text before, text after, baseline) of the words under the rectangle. Glyph positions are only
        estimates (kerning is invisible to us), so whole words are taken and edge punctuation is trimmed."""
        x0, y0, x1, y1 = rect
        sel = [y for x, y, ch, w in self.chars if x0 - pad <= x <= x1 + pad and y0 - 1.5 <= y <= y1 + 0.5]
        if not sel:
            return "", "", "", None
        sel.sort()
        base = sel[len(sel) // 2]
        toks = self._tokens(self.line_at(base, 3.5))

        def hit(t):
            left, right = t[0][0] - t[0][2] / 2, t[-1][0] + t[-1][2] / 2
            over = min(right, x1) - max(left, x0)
            return over > 0 and over >= 0.5 * min(right - left, x1 - x0)

        idx = [i for i, t in enumerate(toks) if hit(t)]
        if not idx:
            return "", "", "", None
        i0, i1 = idx[0], idx[-1]
        words = ["".join(c[1] for c in t) for t in toks]
        text = trim_label(" ".join(words[i0:i1 + 1]))
        before = " ".join(words[:i0])[-30:]
        after = " ".join(words[i1 + 1:])[:30]
        return _norm_text(text), _norm_text(before), _norm_text(after), base

    def snippet(self, y: float | None) -> str:
        """First text line at or just below height y (the top of the page when y is None)."""
        cands = [c for c in self.chars if c[2].strip()]
        if not cands:
            return ""
        top = max(c[1] for c in cands) if y is None else y + 4
        below = [c for c in cands if c[1] <= top and (y is None or c[1] >= y - 70)]
        if not below:
            return ""
        base = max(c[1] for c in below)
        line = [(x, ch, w) for x, cy, ch, w in self.chars if abs(cy - base) <= 2.5]
        return _norm_text(self._join(line))[:100]


def read_links(pdf: Path, pages: set[int] | None = None) -> dict:
    """{"internal": [...], "url": [...]} for links whose page (and, for internal links, destination page) is in
    `pages` (1-based; None = all). Never raises."""
    empty = {"internal": [], "url": []}
    try:
        import logging
        logging.getLogger("pypdf").setLevel(logging.ERROR)  # "fontTools is required ..." etc. are not our business
        from pypdf import PdfReader
    except Exception:  # noqa: BLE001
        return empty
    try:
        with Path(pdf).open("rb") as stream:
            reader = PdfReader(stream)
            return _read(reader, pages)
    except Exception:  # noqa: BLE001  (a damaged annotation tree must never fail a conversion)
        return empty


def _read(reader, pages) -> dict:
    out = {"internal": [], "url": []}
    n_pages = len(reader.pages)
    wanted = [p for p in range(1, n_pages + 1) if pages is None or p in pages]
    ids = {}
    for i, pg in enumerate(reader.pages):
        ref = getattr(pg, "indirect_reference", None)
        if ref is not None:
            ids[ref.idnum] = i + 1
    try:
        named = reader.named_destinations
    except Exception:  # noqa: BLE001
        named = {}
    cache: dict[int, _PageText | None] = {}

    def text_of(p: int):
        if p not in cache:
            try:
                cache[p] = _PageText(reader.pages[p - 1])
            except Exception:  # noqa: BLE001
                cache[p] = None
        return cache[p]

    def resolve(dest):
        """-> (page, y or None) from a destination array/Destination/name; None when unresolvable."""
        try:
            if isinstance(dest, (str, bytes)) or (hasattr(dest, "startswith")):
                key = dest.decode("latin-1") if isinstance(dest, bytes) else str(dest)
                d = named.get(key)
                if d is None:
                    return None
                page_obj, kind, top = d.page, d.typ, d.top
            else:
                dest = dest.get_object() if hasattr(dest, "get_object") else dest
                if hasattr(dest, "get") and "/D" in dest:
                    return resolve(dest["/D"])
                arr = list(dest)
                page_obj, kind = arr[0], str(arr[1])
                top = None
                if kind == "/XYZ" and len(arr) > 3:
                    top = arr[3]
                elif kind in ("/FitH", "/FitBH") and len(arr) > 2:
                    top = arr[2]
                elif kind == "/FitR" and len(arr) > 5:
                    top = arr[5]
            if isinstance(page_obj, int):
                page = page_obj + 1
            else:
                page = ids.get(page_obj.idnum)
            if not page:
                return None
            top = float(top) if top is not None and not isinstance(top, str) and hasattr(top, "__float__") else None
            return page, top
        except Exception:  # noqa: BLE001
            return None

    for p in wanted:
        annots = reader.pages[p - 1].get("/Annots")
        if not annots:
            continue
        for ref in annots:
            try:
                a = ref.get_object()
                if str(a.get("/Subtype")) != "/Link":
                    continue
                rect = tuple(float(v) for v in a["/Rect"])
                rect = (min(rect[0], rect[2]), min(rect[1], rect[3]), max(rect[0], rect[2]), max(rect[1], rect[3]))
                action = a.get("/A")
                action = action.get_object() if action is not None else None
                url = dest = None
                if action is not None:
                    kind = str(action.get("/S"))
                    if kind == "/URI":
                        url = str(action.get("/URI", ""))
                    elif kind == "/GoTo":
                        dest = action.get("/D")
                elif "/Dest" in a:
                    dest = a["/Dest"]
                pt = text_of(p)
                if pt is None:
                    continue
                text, before, after, _ = pt.in_rect(rect)
                if url is not None:
                    out["url"].append({"page": p, "rect": rect, "text": text, "before": before, "after": after, "url": url})
                elif dest is not None:
                    r = resolve(dest)
                    if not r or (pages is not None and r[0] not in pages):
                        continue
                    dp, dy = r
                    dt = text_of(dp)
                    out["internal"].append({
                        "page": p, "rect": rect, "text": text, "before": before, "after": after,
                        "dest_page": dp, "dest_y": dy, "dest_text": dt.snippet(dy) if dt is not None else ""})
            except Exception:  # noqa: BLE001
                continue
    return out


# ---------------------------------------------------------------------------------------------------------------
# Applying links to Markdown
# ---------------------------------------------------------------------------------------------------------------
KIND_WORDS = {
    "figure": "fig", "figures": "fig", "fig.": "fig", "figs.": "fig", "fig": "fig", "table": "tab", "tables": "tab",
    "equation": "eq", "equations": "eq", "eq.": "eq", "eqs.": "eq", "eq": "eq",
    **{w.lower(): p for w, p in PREFIX.items()}, **{w.lower() + "s": p for w, p in PREFIX.items()},
}
_NUMBER = re.compile(r"(?<![\w.-])(" + r"(?:[A-Z][.-]\d+(?:[.-]\d+)*|\d+(?:[.-]\d+)*)[a-z]?" + r")(?![\w]|[.-]\d)")


def _key(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).casefold()
    return "".join(c for c in s if c.isalnum())


def classify(before: str, text: str) -> str | None:
    """Anchor id wanted by a reference such as 'Figure 3.1' (before='… Figure ', text='3.1') or '(3.2)'."""
    t = text.strip()
    m = _NUMBER.search(t)
    if not m:
        return None
    pre = t[:m.start()].strip(" (")
    words = re.findall(r"[A-Za-z.]+", (before + " " + pre))
    kind = None
    if re.fullmatch(r"\(\s*" + NUM + r"\s*\)", t) and not pre:
        kind = "eq"
    for w in reversed(words[-2:]):
        k = KIND_WORDS.get(w.lower()) or KIND_WORDS.get(w.lower().rstrip("."))
        if k:
            kind = k
            break
    if kind is None and re.fullmatch(r"\(\s*" + NUM + r"\s*\)", t):
        kind = "eq"
    return f"{kind}-{norm(m[1])}" if kind else None


STOP = set("""a an the and or but nor of in on at to for from by with without as is are was were be been being it its this
that these those which who whom whose what when where while why how then than there here so if not no yes can could may
might must shall should will would do does did done has have had having also only more most such some any each both all
other another into onto over under about above below between through during before after again further once very just
we our us you your they their them he she his her i me my one two three see per via vs e.g i.e. et al""".split())
_NUMBASE = r"(?:[A-Z][.-])?\d+(?:[.-]\d+)*[a-z]?"
_TOKNUM = re.compile(r"^[(\[]?(" + _NUMBASE + r")")
_ANCHOR_KINDS = set(PREFIX.values()) | {"fig", "tab", "eq"}
KINDW = {**{k: v for k, v in KIND_WORDS.items()}, "section": "sec", "sections": "sec", "sec.": "sec", "secs.": "sec",
         "chapter": "sec", "chapters": "sec", "appendix": "sec", "problem": "prob", "problems": "prob",
         "notebook": "nb", "notebooks": "nb", "algorithm": "alg", "algorithms": "alg", "page": "page", "pages": "page"}
_DEST_LABEL = re.compile(r"^(?P<w>[A-Za-z.]+)\s+(?P<n>" + _NUMBASE + r")")
_DEST_NUM = re.compile(r"^[(\[]?(?P<n>" + _NUMBASE + r")[)\].]?(?:\s|$)")


def _kind_word(tok: str) -> str | None:
    w = tok.strip("()[]:,;\"“”‘’").lower()
    return KINDW.get(w) or KINDW.get(w.rstrip("."))


def trim_label(text: str) -> str:
    """Link text without trailing sentence punctuation or unbalanced brackets/quotes at its edges."""
    t = text.strip()
    t = re.sub(r"^[\"“‘'`]+", "", t)
    while True:
        u = re.sub(r"[.,;:?!\"”’'–—-]+$", "", t)
        if u.count(")") > u.count("(") and u.endswith(")"):
            u = u[:-1]
        if u.count("]") > u.count("[") and u.endswith("]"):
            u = u[:-1]
        if u == t:
            break
        t = u
    if t.count("(") > t.count(")") and t.startswith("("):
        t = t[1:]
    if t.count("[") > t.count("]") and t.startswith("["):
        t = t[1:]
    return t.strip()


def dest_label(snippet: str):
    """(kind or None, number) a destination line announces: 'Figure 3.2 …', '3.1 Heading', '[12] Smith …'."""
    m = _DEST_LABEL.match(snippet.strip())
    if m and _kind_word(m["w"]):
        return _kind_word(m["w"]), m["n"]
    m = _DEST_NUM.match(snippet.strip())
    return (None, m["n"]) if m else None


def _base(n: str) -> str:
    return norm(re.sub(r"[a-z]$", "", n))


def useful_text(text: str) -> bool:
    """A guess without a number may only be linked if it plausibly is a phrase, not a stray word."""
    words = re.findall(r"[A-Za-z0-9\[\]().-]+", text)
    if any(c.isdigit() for c in text):
        return True
    content = [w for w in words if w.lower().strip(".()[]") not in STOP and len(w.strip(".()[]")) > 1]
    return len(words) >= 2 and bool(content)


def number_candidates(item: dict) -> list[dict]:
    """Number-bearing tokens at the guessed position: inside the text first, then the neighbours (the guess is
    often off by a word). Each: label text (kind word included when it is part of the guess), the words before it
    (for matching), the kind its preceding word names (or None) and the number."""
    bt, tt, at = item["before"].split(), item["text"].split(), item["after"].split()
    toks = bt + tt + at
    i0, i1 = len(bt), len(bt) + len(tt) - 1
    order = [i for i in range(i0, i1 + 1)] + [i0 - 1, i1 + 1, i0 - 2, i1 + 2]
    out = []
    for i in order:
        if not 0 <= i < len(toks):
            continue
        tok = trim_label(toks[i])
        m = _TOKNUM.match(tok)
        if not m:
            continue
        prev = toks[i - 1] if i > 0 else ""
        kind = _kind_word(prev)
        start = i
        if kind and i - 1 >= i0 and i - 1 <= i1:
            start = i - 1
        label = " ".join(trim_label(t) if k == i else t for k, t in enumerate(toks[start:i + 1], start))
        out.append({"text": label, "before": " ".join(toks[:start])[-12:], "kind": kind, "num": m[1],
                    "paren": tok.startswith("(")})
    return out


def _find_page_spans(notes: list[dict]) -> dict[int, list[tuple[int, int, int]]]:
    """page -> [(note index, first line, end line)] from the page markers; text before a note's first marker
    belongs to the page that was open at the end of the previous note."""
    spans: dict[int, list[tuple[int, int, int]]] = {}
    cur = None
    for ni, n in enumerate(notes):
        lines = n["body"].split("\n")
        start = 0
        for i, ln in enumerate(lines):
            m = _PAGE_MARK.match(ln)
            if m:
                if cur is not None and i > start:
                    spans.setdefault(cur, []).append((ni, start, i))
                cur, start = int(m[1]), i + 1
        if cur is not None and len(lines) > start:
            spans.setdefault(cur, []).append((ni, start, len(lines)))
    return spans


def _blocks_in(lines: list[str], a: int, b: int):
    """Blocks (absolute start, end, kind) of lines[a:b], with fences/math handled."""
    for s, e, kind in split_blocks(lines[a:b]):
        yield a + s, a + e, kind


def _url_ok(url: str) -> bool:
    u = url.strip()
    if not u or re.search(r"[\s<>\"]", u):
        return False
    scheme = urllib.parse.urlparse(u).scheme.lower()
    return scheme in _SCHEMES


def _md_url(url: str) -> str:
    return urllib.parse.quote(url, safe=":/?#[]@!$&'*+,;=%-._~").replace("(", "%28").replace(")", "%29")


def _pattern(text: str) -> re.Pattern | None:
    toks = text.split()
    if not toks or len(text) > 120:
        return None
    body = r"\s+".join(re.escape(t) for t in toks)
    pre = r"(?<![A-Za-z0-9])" if text[0].isalnum() else ""
    if text[0].isdigit():
        pre += r"(?<!\d\.)"
    post = r"(?![A-Za-z0-9])(?!\.\d)" if text[-1].isalnum() else ""
    return re.compile(pre + body + post)


def _slug(text: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode())
    return "-".join(w.lower() for w in words[:3])[:24].strip("-") or "x"


def apply_links(notes: list[dict], links: dict, plain: bool, registry: dict | None = None,
                internal: bool = True, urls: bool = True) -> dict:
    """Link text on the converted pages. `notes` bodies must still contain the page markers. `registry` maps the
    anchor ids of the pattern pass ("eq-3-2") to note stems. Returns {"pdf_links": n, "url_links": n}."""
    registry = registry if registry is not None else {}
    counts = {"pdf_links": 0, "url_links": 0}
    spans = _find_page_spans(notes)
    if not spans:
        return counts
    files = [n["file"] for n in notes]
    note_lines = [n["body"].split("\n") for n in notes]

    # --- per page: prose lines (masked) in document order ------------------------------------------------------
    page_cache: dict[int, list[tuple[int, int, str, str]]] = {}

    def prose(page: int, urls_mask: bool):
        key = (page, urls_mask)
        if key not in page_cache:
            rows = []
            for ni, a, b in spans.get(page, []):
                lines = note_lines[ni]
                for s, e, kind in _blocks_in(lines, a, b):
                    if kind not in ("para", "quote", "list"):
                        continue
                    text = "\n".join(lines[s:e])
                    masked = _protect(text, None, urls_mask).split("\n")
                    for off, ml in enumerate(masked):
                        rows.append((ni, s + off, lines[s + off], ml))
            page_cache[key] = rows
        return page_cache[key]

    # --- anchors on page D (existing ids by line) ---------------------------------------------------------------
    id_line = re.compile(r"(?:^|[ \t])\^([A-Za-z0-9-]+)[ \t]*$")

    def anchor_page(ident: str):
        """Page whose span contains the anchor for `ident` (pattern pass), or None."""
        stem = registry.get(ident)
        if stem is None or stem not in files:
            return None
        ni = files.index(stem)
        lines = note_lines[ni]
        for i, ln in enumerate(lines):
            if f"^{ident}" in ln or f'<a id="{ident}"></a>' in ln:
                for page, sp in spans.items():
                    for sni, a, b in sp:
                        if sni == ni and a <= i < b:
                            return page
        return None

    # --- block / heading lookup on the destination page ---------------------------------------------------------
    def heading_target(page: int, snippet: str):
        sk = _key(snippet)
        if len(sk) < 4:
            return None
        for ni, a, b in spans.get(page, []):
            lines = note_lines[ni]
            for s, e, kind in _blocks_in(lines, a, b):
                if kind != "heading":
                    continue
                m = _HEAD.match(lines[s].strip())
                if not m:
                    continue
                hk = _key(m[1])
                if len(hk) >= 4 and (sk.startswith(hk) or hk.startswith(sk[:max(4, min(len(sk), len(hk)))])
                                     and len(sk) >= 4 and sk[:len(hk)] == hk[:len(sk)]):
                    return ni, m[1].strip()
        return None

    new_ids: dict[tuple[int, int], tuple[str, int]] = {}   # (note, first line) -> (new block id, last line)
    used_ids = set(registry) | {m[1] for ln in note_lines for l in ln for m in [id_line.search(l)] if m}

    def block_target(page: int, snippet: str):
        """Existing or new block id for the paragraph/list item on `page` that starts like `snippet`."""
        sk = _key(snippet)
        if len(sk) < 8:
            return None
        sk = sk[:40]
        best = None
        for ni, a, b in spans.get(page, []):
            lines = note_lines[ni]
            for s, e, kind in _blocks_in(lines, a, b):
                if kind not in ("para", "list"):
                    continue
                starts = [s] if kind == "para" else [i for i in range(s, e) if re.match(r"\s*([-*+]|\d+[.)])\s", lines[i])]
                for st in starts:
                    text = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", lines[st])
                    if st + 1 < e and kind == "para":
                        text += " " + " ".join(lines[st + 1:e])
                    bk = _key(text)[:40]
                    if len(bk) < 8:
                        continue
                    r = difflib.SequenceMatcher(None, sk[:len(bk)], bk[:len(sk)]).ratio()
                    if r >= 0.85 and (best is None or r > best[0]):
                        last = (e - 1) if kind == "para" else st
                        best = (r, ni, st, last, kind)
        if not best:
            return None
        _, ni, st, last, kind = best
        lines = note_lines[ni]
        have = id_line.search(lines[last])
        if have:
            return files[ni], have[1]
        anchor = re.search(r'<a id="([^"]+)"></a>', lines[st])   # plain flavour: inline (list item) or line before
        k = st - 1
        while k >= 0 and not lines[k].strip():
            k -= 1
        if not anchor and k >= 0:
            anchor = re.fullmatch(r'\s*<a id="([^"]+)"></a>\s*', lines[k])
        if anchor:
            return files[ni], anchor[1]
        if (ni, st) in new_ids:
            return files[ni], new_ids[(ni, st)][0]
        ident = f"p{page}-{_slug(snippet)}"
        base, k = ident, 2
        while ident in used_ids:
            ident, k = f"{base}-{k}", k + 1
        used_ids.add(ident)
        new_ids[(ni, st)] = (ident, last)
        return files[ni], ident

    # --- group annotations per source page, reading order ------------------------------------------------------
    def reading_order(items):
        return sorted(items, key=lambda r: (-round(r["rect"][3] / 4), r["rect"][0]))

    edits: dict[tuple[int, int], list[tuple[int, int, str]]] = {}   # (note, line) -> [(start, end, replacement)]
    consumed: dict[tuple[int, int], list[tuple[int, int]]] = {}

    def free(key, s, e):
        return all(e <= a or s >= b for a, b in consumed.get(key, []))

    def locate(rows, item, seen: dict, want: str | None = None):
        """Occurrence (note, line, start, end) of the link text on the page, k-th per identical needle."""
        text = item["text"]
        pat = _pattern(text)
        if not pat:
            return None
        ctx = (item["before"][-12:] + " " + text).strip() if item["before"] else ""
        needles = [(ctx, len(item["before"][-12:]) + 1)] if ctx and ctx != text else []
        needles.append((text, 0))
        for needle, skip in needles:
            npat = _pattern(needle)
            if not npat:
                continue
            occ = []
            for ni, ln, raw, masked in rows:
                for m in npat.finditer(masked):
                    s = m.start() + (skip if needle != text else 0)
                    e = m.end()
                    if needle != text:
                        # the text part starts after the context; align on the last match of the text itself
                        t = pat.search(masked, m.start() + max(0, skip - 1))
                        if not t or t.end() != m.end():
                            continue
                        s = t.start()
                    if "\0" not in masked[s:e] and free((ni, ln), s, e):
                        if want:  # the word before the number in the text must not name another kind of target
                            w = re.search(r"([A-Za-z.]+)[ \t(\u00a0]*$", raw[:s])
                            pk = _kind_word(w[1]) if w else None
                            if pk and pk != want:
                                continue
                        occ.append((ni, ln, s, e))
            if occ:
                k = seen.get(needle, 0)
                seen[needle] = k + 1
                return occ[k] if k < len(occ) else None
        return None

    by_page: dict[int, list[dict]] = {}
    for kind, items in (("internal", links.get("internal", []) if internal else []), ("url", links.get("url", []) if urls else [])):
        for it in items:
            if it.get("text"):
                by_page.setdefault(it["page"], []).append({**it, "kind": kind})

    for page in sorted(by_page):
        if page not in spans:
            continue
        seen_i: dict = {}
        seen_u: dict = {}
        for it in reading_order(by_page[page]):
            if it["kind"] == "url":
                if not _url_ok(it["url"]):
                    continue
                words = trim_label(it["text"]).split()
                while words and words[0].lower().strip("()") in STOP:     # a guess shifted by a word drags stopwords in
                    words.pop(0)
                while words and words[-1].lower().strip("()") in STOP:
                    words.pop()
                it = {**it, "text": trim_label(" ".join(words))}
                if not it["text"]:
                    continue
                rows = prose(page, False)
                at = locate(rows, it, seen_u)
                if not at:
                    continue
                ni, ln, s, e = at
                raw = note_lines[ni][ln]
                label = raw[s:e]
                label = label.replace("[", "\\[").replace("]", "\\]")
                edits.setdefault((ni, ln), []).append((s, e, f"[{label}]({_md_url(it['url'].strip())})"))
                consumed.setdefault((ni, ln), []).append((s, e))
                counts["url_links"] += 1
                continue
            dest_page, snippet = it["dest_page"], it["dest_text"]
            label_info = dest_label(snippet)

            def anchor_for(ident):
                if ident in registry:
                    ap = anchor_page(ident)
                    if ap is not None and abs(ap - dest_page) <= 1:
                        return registry[ident], "^" + ident
                return None

            def route(c):
                """Target for one number candidate; never an anchor of another kind than the word before it names."""
                kind, num = c["kind"], c["num"]
                if kind in _ANCHOR_KINDS:
                    t = anchor_for(f"{kind}-{_base(num)}")
                    if t:
                        return t
                    if not (label_info and label_info[0] == kind and _base(label_info[1]) == _base(num)):
                        return None
                    b = block_target(dest_page, snippet)
                    return (b[0], "^" + b[1]) if b else None
                if kind == "page":
                    return None
                if kind is None and c["paren"]:
                    t = anchor_for(f"eq-{_base(num)}")
                    if t:
                        return t
                want = kind or ("eq" if c["paren"] else None)   # "(3.2)" reads as an equation number
                if not label_info or _base(label_info[1]) != _base(num) or (want and label_info[0] not in (None, want)):
                    return None
                if not plain:
                    h = heading_target(dest_page, snippet)
                    if h and not _NO_LINK.search(h[1]):
                        return files[h[0]], h[1]
                b = block_target(dest_page, snippet)
                return (b[0], "^" + b[1]) if b else None

            picked = None
            for c in number_candidates(it):
                t = route(c)
                if not t:
                    continue
                at = locate(prose(page, True), {**it, "text": c["text"], "before": c["before"]}, seen_i,
                            c["kind"] or ("eq" if c["paren"] else None))
                if at:
                    picked = (at, t)
                    break
            if picked is None and not number_candidates(it) and useful_text(trim_label(it["text"])):
                txt = trim_label(it["text"])
                t = None
                if not plain:
                    h = heading_target(dest_page, snippet)
                    if h and not _NO_LINK.search(h[1]):
                        t = (files[h[0]], h[1])
                if t is None:
                    b = block_target(dest_page, snippet)
                    t = (b[0], "^" + b[1]) if b else None
                if t:
                    at = locate(prose(page, True), {**it, "text": txt}, seen_i)
                    if at:
                        picked = (at, t)
            if picked is None:
                continue
            (ni, ln, s, e), (stem, frag) = picked
            raw = note_lines[ni][ln]
            label = raw[s:e]
            here = files[ni]
            if plain:
                ident_only = frag.lstrip("^")
                url = ("" if stem == here else urllib.parse.quote(stem + ".md", safe="/#=")) + "#" + ident_only
                rep = f"[{label.replace('[', chr(92) + '[').replace(']', chr(92) + ']')}]({url})"
            elif re.search(r"[\[\]|]", label):
                url = ("" if stem == here else urllib.parse.quote(stem + ".md", safe="/#=")) + "#" + urllib.parse.quote(frag, safe="^")
                rep = f"[{label.replace('[', chr(92) + '[').replace(']', chr(92) + ']')}]({url})"
            else:
                rep = f"[[{'' if stem == here else stem}#{frag}|{label}]]"
            edits.setdefault((ni, ln), []).append((s, e, rep))
            consumed.setdefault((ni, ln), []).append((s, e))
            counts["pdf_links"] += 1

    # --- apply: inline replacements first (never shift line numbers), then new block ids ------------------------
    for (ni, ln), reps in edits.items():
        line = note_lines[ni][ln]
        for s, e, rep in sorted(reps, reverse=True):
            line = line[:s] + rep + line[e:]
        note_lines[ni][ln] = line
    anchors: dict[int, list[tuple[int, str, int]]] = {}
    for (ni, st), (ident, last) in new_ids.items():
        anchors.setdefault(ni, []).append((st, ident, last))
    for ni, items in anchors.items():
        lines = note_lines[ni]
        for st, ident, last in sorted(items, reverse=True):
            is_item = re.match(r"\s*([-*+]|\d+[.)])\s", lines[st]) is not None
            if plain:
                if is_item:
                    lines[st] = re.sub(r"^(\s*(?:[-*+]|\d+[.)])\s+)", lambda m: m[1] + f'<a id="{ident}"></a>', lines[st], 1)
                else:
                    ins = [f'<a id="{ident}"></a>', ""]
                    if st > 0 and lines[st - 1].strip():
                        ins.insert(0, "")
                    lines[st:st] = ins
            else:
                tgt = st if is_item else last
                lines[tgt] += f" ^{ident}"
    for n, lines in zip(notes, note_lines):
        n["body"] = "\n".join(lines)
    return counts


