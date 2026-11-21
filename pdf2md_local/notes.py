"""Output: cleaning Paddle Markdown, chapter splitting, note rendering, safe file names.
"""
from __future__ import annotations

import html.parser
import json
import re
import shutil
import urllib.parse
from pathlib import Path

from pdf2md_local.latex import simplify_light_math, text_macros_to_unicode
from pdf2md_local.outline import HEADING, wants_page_link

AMP_ENTITY = re.compile(r"&amp;(?!(?:[A-Za-z][A-Za-z0-9]*|#\d+|#x[0-9A-Fa-f]+);)")


def unescape_entities(md: str) -> str:
    """OCR output HTML-escapes '&' in text; undo that outside code (a literal &amp;nbsp; is left alone)."""
    out, in_code = [], False
    for line in md.split("\n"):
        if line.strip().startswith(("```", "~~~")):
            in_code = not in_code
        out.append(line if in_code else AMP_ENTITY.sub("&", line))
    return "\n".join(out)


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


_YAML_UNSAFE = re.compile("[\x7f-\x9f\u2028\u2029\ufeff\ud800-\udfff]")


def yaml_str(value) -> str:
    """Any text as a YAML double-quoted scalar (JSON strings are valid YAML); odd line breaks and controls escaped."""
    return _YAML_UNSAFE.sub(lambda m: "\\u%04x" % ord(m.group(0)), json.dumps(str(value), ensure_ascii=False))


FAILED_MARKER = re.compile(r"<!-- pdf2md:failed page=(\d+) -->")


def failed_marker(page: int) -> str:
    return f"<!-- pdf2md:failed page={page} -->"


def render_note(note: dict, book: str, source_pdf: str, prev: str | None, nxt: str | None,
                index: str, date: str, warnings: int) -> str:
    heading = f"# {note['chapter']} {note['title']}" if note["chapter"] else f"# {note['title']}"
    nav = " | ".join(x for x in (f"[[{prev}|Previous]]" if prev else "", f"[[{index}|Contents]]",
                                  f"[[{nxt}|Next]]" if nxt else "") if x)
    front = (f"---\ntitle: {yaml_str(note['title'])}\nbook: {yaml_str(book)}\n"
             + (f"chapter: {note['chapter']}\n" if note["chapter"] else "")
             + f"source_pdf: {yaml_str(source_pdf)}\nconverted: {date}\nmath_warnings: {warnings}\ntags: [pdf_import]\n---\n\n")
    return front + nav + "\n\n" + heading + "\n\n" + note["body"].strip("\n") + "\n"


def render_index(notes: list[dict], book: str, source_pdf: str, date: str, pages_failed: list[int] | None = None,
                 pages: str | None = None) -> str:
    rows = []
    for n in notes:
        label = f"{n['chapter']}. {n['title']}" if n["chapter"] else n["title"]
        rows.append(f"- [[{n['file']}|{label}]]")
    return (f"---\ntitle: {yaml_str(book)}\nsource_pdf: {yaml_str(source_pdf)}\nconverted: {date}\n"
            + (f"pages: {yaml_str(pages)}\n" if pages else "")
            + (f"pages_failed: {sorted(pages_failed)}\n" if pages_failed else "")
            + f"tags: [pdf_import]\n---\n\n# {book}\n\n" + "\n".join(rows) + "\n")


_PAGE_NUM = re.compile(r"<!-- pdf2md:page=(\d+) -->")
_TRAILING_PAGE = re.compile(r"<!-- pdf2md:page=\d+ -->\s*$")


def split_book_paged(md: str, book: str, keep_markers: bool = False) -> list[dict] | None:
    """split_book on text that still has its page markers: each note gets "pages" (the pages that start in it; a
    marker at the very end belongs to the next note). Markers are then removed from the bodies unless kept."""
    notes = split_book(md, book)
    if not notes:
        return None
    for n in notes:
        n["pages"] = [int(x) for x in _PAGE_NUM.findall(n["body"])]
    for i, n in enumerate(notes[:-1]):
        if n["pages"] and _TRAILING_PAGE.search(n["body"]):
            notes[i + 1]["pages"].insert(0, n["pages"].pop())
    if not keep_markers:
        strip_note_markers(notes)
    return notes


def strip_note_markers(notes: list[dict]) -> None:
    """Remove the page markers from every note body (in place); an emptied front-matter note is dropped."""
    for n in notes:
        n["body"] = strip_page_markers(n["body"] + "\n").strip("\n")
    if notes and notes[0]["kind"] == "front" and not notes[0]["body"].strip():
        notes.pop(0)


def note_index_for_page(notes: list[dict], page: int) -> int:
    """Index of the note that holds a PDF page (the note whose pages include it, else the last one starting before)."""
    for i, n in enumerate(notes):
        if page in n.get("pages", ()):
            return i
    best = 0
    for i, n in enumerate(notes):
        if n.get("pages") and n["pages"][0] <= page:
            best = i
    return best


def unique_dir(p: Path) -> Path:
    q, n = p, 2
    while q.exists():
        q, n = p.with_name(f"{p.name} ({n})"), n + 1
    return q


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


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    n = 2
    while (q := p.with_name(f"{p.stem} ({n}){p.suffix}")).exists():
        n += 1
    return q


PAGE_MARKER = re.compile(r"^(?:>[ \t]?)*<!-- pdf2md:page=\d+ -->[ \t]*\n(?:(?:>[ \t]*)?[ \t]*\n)*", re.M)  # also inside a callout


def strip_page_markers(md: str) -> str:
    """Remove the hidden per-page comments (and the blank lines after them); they are only needed for heading repair."""
    return PAGE_MARKER.sub("", md)


def to_plain(text: str, assets_rel: str, pdf_rel: str) -> str:
    """Obsidian syntax -> standard Markdown: ![[x.png]] -> ![](assets/x.png), [[a|b]] -> [b](a.md), PDF page links."""
    q = lambda v: urllib.parse.quote(v, safe="/#=")
    text = re.sub(r"\[\[[^\]|#]+#page=(\d+)\|([^\]]+)\]\]", lambda m: f"[{m[2]}]({q(pdf_rel)}#page={m[1]})", text)
    text = re.sub(r"!\[\[([^\]|]+)\]\]", lambda m: f"![{Path(m[1]).stem}]({q(assets_rel + '/' + m[1])})", text)
    return re.sub(r"\[\[([^\]|#]+)\|([^\]]+)\]\]", lambda m: f"[{m[2]}]({q(m[1] + '.md')})", text)
