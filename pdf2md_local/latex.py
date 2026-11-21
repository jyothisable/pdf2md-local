"""LaTeX tidying, normalisation, validation and Unicode simplification of maths.
"""
from __future__ import annotations

import re

CHUNK_CHARS = 5000
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
    """Fix formula artefacts: spacing, stray & / equation numbers."""
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
