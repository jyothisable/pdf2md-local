"""Heading repair and PDF-outline matching.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from pathlib import Path

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
