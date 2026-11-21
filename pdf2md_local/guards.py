"""Source-backed guards for formulas, prose and figures (protect_paddle_result).
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Callable

from pdf2md_local.latex import DOLLAR_INLINE, TEXT_MACROS, blank_text_groups, latex_problems, tidy_block
from pdf2md_local.outline import heading_key


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
