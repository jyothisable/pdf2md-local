"""Theorem-like environments (Theorem, Lemma, Definition, Proof, ...) as Obsidian callouts or plain blockquotes."""
from __future__ import annotations

import re

CALLOUT_TYPES = {
    "Theorem": "theorem", "Lemma": "lemma", "Proposition": "proposition", "Corollary": "corollary",
    "Definition": "definition", "Example": "example", "Remark": "note", "Exercise": "question", "Proof": "proof",
}
_WORDS = "|".join(CALLOUT_TYPES)
# optional bold/italic, label, optional number, optional (name), '.' or ':' (markup may close before or after it)
LABEL = re.compile(
    r"^(?P<open>(?:\*\*\*|\*\*|\*|__|_)?)(?P<word>" + _WORDS + r")\b"
    r"(?:[ \t]+(?P<num>[A-Z]?\d+(?:\.\d+)*[A-Za-z]?|[A-Z](?:\.\d+)+))?"
    r"(?P<close0>\*\*\*|\*\*|\*|__|_)?"
    r"(?:[ \t]*\((?P<name>[^()\n]*(?:\([^()\n]*\)[^()\n]*)*)\))?"
    r"(?P<close1>\*\*\*|\*\*|\*|__|_)?[ \t]*(?P<punct>[.:])(?P<close2>\*\*\*|\*\*|\*|__|_)?(?=\s|$)")
END_MARK = re.compile(r"(?:□|∎|■|\$\\square\$|\$\\blacksquare\$|\\\(\\square\\\)|\$\s*\\(?:square|blacksquare|Box)\s*\$)\s*$")
_MD_CLOSE = re.compile(r"^(\*\*\*|\*\*|\*|__|_)")


PAGE_MARKER_LINE = re.compile(r"^\s*<!-- pdf2md:page=\d+ -->\s*$")


def parse_label(line: str):
    """Match a recognised label at the start of a paragraph line -> (match, canonical word) or None."""
    m = LABEL.match(line)
    if not m:
        return None
    # markup must be balanced: an opening marker needs a closing one somewhere in the label part
    closes = m["close0"] or m["close1"] or m["close2"]
    if m["open"] and not closes:
        return None
    if closes and not m["open"]:
        return None
    # a bare "Example." with no number is fine; "Proof of Theorem 2" is not matched (needs . or : after label)
    return m


def label_text(m) -> str:
    """'Theorem 2.1 (Name)' without markup and punctuation."""
    s = m["word"]
    if m["num"]:
        s += " " + m["num"]
    if m["name"] is not None and m["name"].strip():
        s += f" ({m['name'].strip()})"
    return s


def split_blocks(lines: list[str]):
    """Yield (start, end_exclusive, kind) over `lines`. Kinds: blank, math, code, comment, heading, table, image,
    quote, list, para. Display math and fenced code are kept whole; other blocks are runs of non-blank lines."""
    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if not s:
            j = i
            while j < n and not lines[j].strip():
                j += 1
            yield i, j, "blank"
            i = j
        elif s.startswith("```") or s.startswith("~~~"):
            fence = s[:3]
            j = i + 1
            while j < n and not lines[j].strip().startswith(fence):
                j += 1
            yield i, min(j + 1, n), "code"
            i = min(j + 1, n)
        elif s.startswith("$$"):
            if len(s) > 2 and s.endswith("$$") and s != "$$":
                yield i, i + 1, "math"
                i += 1
                continue
            j = i + 1
            while j < n and "$$" not in lines[j]:
                j += 1
            if j >= n:  # unterminated: treat the opener as a paragraph line
                yield i, i + 1, "para"
                i += 1
            else:
                yield i, j + 1, "math"
                i = j + 1
        else:
            kind = _line_kind(lines[i])
            if kind in ("heading", "comment", "image"):
                yield i, i + 1, kind
                i += 1
                continue
            j = i + 1
            while j < n and lines[j].strip() and not lines[j].lstrip().startswith(("$$", "```", "~~~")):
                k2 = _line_kind(lines[j])
                if k2 != kind and not (kind == "list" and k2 == "para"):
                    break
                j += 1
            yield i, j, kind
            i = j


def _line_kind(line: str) -> str:
    s = line.strip()
    if re.match(r"#{1,6}\s", s):
        return "heading"
    if s.startswith("<!--") and s.endswith("-->"):
        return "comment"
    if re.fullmatch(r"!\[\[[^\]]+\]\]|!\[[^\]]*\]\([^)]*\)", s):
        return "image"
    if s.startswith("|"):
        return "table"
    if s.startswith("<table") or s.startswith("</table"):
        return "table"
    if s.startswith(">"):
        return "quote"
    if re.match(r"([-*+]|\d+[.)])\s", s):
        return "list"
    return "para"


def apply_callouts(md: str, plain: bool) -> tuple[str, int]:
    """Wrap labelled environments in callouts (obsidian) or bold-label blockquotes (plain). Returns (md, count).
    Idempotent: already quoted blocks are never labelled again."""
    lines = md.split("\n")
    blocks = list(split_blocks(lines))
    out: list[str] = []
    count = 0
    k = 0
    while k < len(blocks):
        a, b, kind = blocks[k]
        m = parse_label(lines[a]) if kind == "para" else None
        if not m:
            out.extend(lines[a:b])
            k += 1
            continue
        word = m["word"]
        is_proof = word == "Proof"
        cap = 12 if is_proof else 6
        # collect content blocks (skipping blanks) after the label paragraph
        members = [k]
        j = k + 1
        closed = bool(is_proof and END_MARK.search(lines[b - 1].rstrip()))
        capped = False
        pending: list[int] = []
        while not closed and j < len(blocks):
            ja, jb, jk = blocks[j]
            if jk == "blank":
                j += 1
                continue
            if jk in ("heading", "table", "image", "quote", "code", "list"):
                break
            if jk == "para" and parse_label(lines[ja]):
                break
            if jk == "comment":
                if PAGE_MARKER_LINE.match(lines[ja]):  # a page boundary is not a stop; kept inside if text follows
                    pending.append(j)
                    j += 1
                    continue
                break
            if sum(1 for q in members if blocks[q][2] != "comment") >= cap:
                capped = True
                break
            members.extend(pending)
            pending = []
            members.append(j)
            if jk == "para" and is_proof and END_MARK.search(lines[jb - 1].rstrip()):
                closed = True
            j += 1
        if capped:
            # cap reached: label paragraph and its directly following display math only
            members = [k]
            t = k + 1
            while t < len(blocks) and blocks[t][2] in ("blank", "math"):
                if blocks[t][2] == "math":
                    members.append(t)
                t += 1
        last = members[-1]
        # merge members with blank lines between them (blank runs inside become ">")
        body: list[str] = []
        for idx in range(members[0], last + 1):
            ba, bb, bk = blocks[idx]
            if bk == "blank":
                body.append("")
            else:
                body.extend(lines[ba:bb])
        # label rewriting
        first = body[0]
        rest_of_first = first[m.end():].lstrip()
        text = label_text(m)
        if plain:
            head = f"> **{text}{m['punct']}**" + (f" {rest_of_first}" if rest_of_first else "")
            out.append(head)
        else:
            out.append(f"> [!{CALLOUT_TYPES[word]}] {text}")
            if rest_of_first:
                out.append(f"> {rest_of_first}")
        for ln in body[1:]:
            out.append(f"> {ln}" if ln.strip() else ">")
        count += 1
        k = last + 1
    return "\n".join(out), count
