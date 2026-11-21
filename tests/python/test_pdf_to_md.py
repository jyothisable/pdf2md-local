"""Tests for the conversion core (no OCR model needed)."""
import importlib.util
import re
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pdf2md_local import convert as m  # noqa: E402


class TidyTests(unittest.TestCase):
    def test_letter_spaced_words_and_numbers(self):
        self.assertEqual(m.tidy_block("A t t e n t i o n ( Q , K , V )"), "Attention (Q, K, V)")
        self.assertEqual(m.tidy_block("d _ { \\min } ^ { - 0 . 5 }"), "d_{\\min}^{-0.5}")
        self.assertEqual(m.tidy_block("10 0 0 0 ^ { 2 i }"), "10000^{2 i}")

    def test_trailing_equation_number_becomes_tag(self):
        self.assertEqual(m.tidy_block("a = b & & ( 1 )"), "a = b \\tag{1}")

    def test_alignment_gets_environment(self):
        out = m.tidy_block("a & = b \\\\ c & = d")
        self.assertTrue(out.startswith("\\begin{aligned}") and out.endswith("\\end{aligned}"))
        self.assertEqual(m.latex_problems(out), [])

    def test_text_groups_keep_their_spaces(self):
        self.assertEqual(m.tidy_block("\\text {where head} _ { i }"), "\\text{where head}_{i}")

    def test_macros_not_glued_to_following_letters(self):
        self.assertEqual(m.tidy_block("\\alpha b + \\sin x"), "\\alpha b + \\sin x")

    def test_idempotent(self):
        for s in ["A t t e n t i o n ( x ) & & ( 3 )", "a & b \\\\ c & d", "\\frac { Q K ^ { T } } { \\sqrt { d _ { k } } }"]:
            once = m.tidy_block(s)
            self.assertEqual(m.tidy_block(once), once)


class ValidateTests(unittest.TestCase):
    def test_arrows_are_not_left_or_right_delimiters(self):
        # regression: \\leftarrow / \\rightarrow were counted as \\left / \\right, so every equation with an arrow looked broken
        for s in [r"\pi \leftarrow x", r"a \rightarrow b", r"a \leftrightarrow b \Rightarrow c",
                  r"\left( a \rightarrow b \right)", r"x \leftarrow \left[ y \right]", r"a \rightleftharpoons b",
                  r"\left\{ a \leftarrow b \right\}", r"\overleftarrow{x} \overrightarrow{y}"]:
            self.assertEqual(m.latex_problems(s), [], s)
        self.assertEqual(m.latex_problems(r"\left( a \leftarrow b"), ["\\left/\\right count differs"])
        self.assertEqual(m.latex_problems(r"a \right) b"), ["\\left/\\right count differs"])

    def test_dollar_inside_text_groups_is_valid(self):
        for s in [r"\text{ for $\vec{v}_{x+1}$ and $y$ }", r"a \text{where {nested $q$} ok} + b",
                  r"\begin{aligned} \text{see $x$} & = 1 \\ y & = 2 \end{aligned}"]:
            self.assertEqual(m.latex_problems(s), [], s)
        self.assertTrue(m.latex_problems(r"\text{ok} $ \frac{a}{b}"))           # a stray $ outside text still counts
        self.assertTrue(m.latex_problems(r"\text{unbalanced {x} + \frac{a}{b}"))  # unbalanced text group is reported

    def test_good(self):
        for s in ["x^2", "\\frac{a}{b}", "\\left( x \\right)", "\\begin{aligned} a & b \\end{aligned}",
                  "\\{ x \\}", "\\substack{a \\\\ b}", "a \\$ b"]:
            self.assertEqual(m.latex_problems(s), [], s)

    def test_bad(self):
        cases = {"\\frac{a}{b": "unclosed {", "a}": "unbalanced }", "a & b": "outside an environment",
                 "\\begin{aligned} a": "unclosed", "\\left( x": "\\left/\\right", "": "empty",
                 "<!-- formula-not-decoded -->": "not decoded", "\\begin{a} x \\end{b}": "mismatched"}
        for s, want in cases.items():
            self.assertTrue(any(want in p for p in m.latex_problems(s)), s)

    def test_document_issues_and_code_fences(self):
        md = "ok $x^2$ bad $\\frac{a$\n\n$$\na & b\n$$\n\n```\n$ \\frac{ $\n```\n\n$$\nx\n"
        reasons = [r for _, r, _ in m.validate_document(md)]
        self.assertTrue(any("unclosed {" in r for r in reasons))
        self.assertTrue(any("outside an environment" in r for r in reasons))
        self.assertIn("unclosed $$ block", reasons)
        self.assertEqual(len(reasons), 3)  # the fenced code is ignored


class NormalizeTests(unittest.TestCase):
    def test_delimiters(self):
        out = m.normalize_math("Text \\(x^2\\) and $ a+b $, cost \\$5.\n\\[ E = mc^2 \\]\nHere $$a=b$$ end.")
        self.assertIn("$x^2$", out)
        self.assertIn("$a+b$", out)
        self.assertIn("\\$5", out)
        self.assertIn("\n$$\nE = mc^2\n$$\n", out)
        self.assertIn("\n$$\na=b\n$$\n", out)

    def test_code_fence_untouched(self):
        src = "```\n$ keep $ \\(x\\)\n```\n"
        self.assertEqual(m.normalize_math(src), src)

    def test_idempotent(self):
        src = "a \\(x\\)\n\n$$\nA t t & & ( 1 )\n$$\n\n\\[y\\]"
        once = m.normalize_math(src)
        self.assertEqual(m.normalize_math(once), once)








class OutputTests(unittest.TestCase):
    def test_unique_path_never_collides(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.md"
            self.assertEqual(m.unique_path(p), p)
            p.write_text("x")
            self.assertEqual(m.unique_path(p).name, "a (2).md")
            (Path(d) / "a (2).md").write_text("x")
            self.assertEqual(m.unique_path(p).name, "a (3).md")

    def test_attention_paper_regressions(self):
        raw = ("$$\nA t t e n t i o n ( Q , K , V ) = \\text {softmax} ( \\frac { Q K ^ { T } } "
               "{ \\sqrt { d _ { k } } } ) V & & ( 1 )\n$$\n\n$$\nP E _ { ( p o s , 2 i ) } & = s i n "
               "( p o s / 1 0 0 0 0 ^ { 2 i } ) \\\\ P E _ { ( p o s , 2 i + 1 ) } & = \\cos ( p o s )\n$$\n")
        out = m.normalize_math(raw)
        self.assertEqual(m.validate_document(out), [])
        self.assertIn("\\tag{1}", out)
        self.assertIn("Attention (", out)
        self.assertIn("\\begin{aligned}", out)






class EntityTests(unittest.TestCase):
    def test_unescapes_ampersand_only_where_safe(self):
        self.assertEqual(m.unescape_entities("EU &amp; Rome, R&amp;D"), "EU & Rome, R&D")
        self.assertEqual(m.unescape_entities("literal &amp;nbsp; and &amp;#39;"), "literal &amp;nbsp; and &amp;#39;")
        self.assertEqual(m.unescape_entities("a &lt; b"), "a &lt; b")
        self.assertEqual(m.unescape_entities("```\n&amp;\n```\n&amp;"), "```\n&amp;\n```\n&")
        once = m.unescape_entities("x &amp; y")
        self.assertEqual(m.unescape_entities(once), once)


class HeadingTests(unittest.TestCase):
    def test_levels_follow_numbering(self):
        src = "# Title\n\n# 3.4 Embeddings\n\n## 3 Model\n\n## 2.1.3. Deep\n\n### Plain heading\n\n```\n# 1.2 code\n```\n"
        out = m.fix_headings(src)
        self.assertIn("## 3.4 Embeddings", out)
        self.assertIn("# 3 Model", out)
        self.assertIn("### 2.1.3. Deep", out)
        self.assertIn("### Plain heading", out)
        self.assertIn("# Title", out)
        self.assertIn("```\n# 1.2 code\n```", out)
        self.assertEqual(m.fix_headings(out), out)




class CheckpointTests(unittest.TestCase):
    def test_cache_round_trip_and_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            c = m.Cache(Path(d) / "k")
            self.assertIsNone(c.get("a"))
            c.put("a", {"md": "x\u00e9", "n": 1})
            self.assertEqual(c.get("a"), {"md": "x\u00e9", "n": 1})
            (Path(d) / "k" / "b.json").write_text("{broken")
            self.assertIsNone(c.get("b"))          # corrupt entries are ignored, not fatal
            self.assertEqual(list((Path(d) / "k").glob("*.tmp")), [])


    def test_run_key_changes_with_options(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.pdf"
            f.write_bytes(b"%PDF")
            self.assertEqual(m.run_key(f, 1, 2), m.run_key(f, 1, 2))
            self.assertNotEqual(m.run_key(f, 1, 2), m.run_key(f, 1, 3))
            f.write_bytes(b"%PDF-changed")
            self.assertNotEqual(m.run_key(f, 1, 2), m.run_key(Path(d) / "a.pdf", 1, 3))


class ResilienceTests(unittest.TestCase):
    def make(self, bad_pages):
        def convert(rng):
            lo, hi = rng
            if any(lo <= p <= hi for p in bad_pages):
                raise RuntimeError("boom")
            s = m.new_stats()
            s["eq_total"] = hi - lo + 1
            return "".join(f"page {p}\n\n" for p in range(lo, hi + 1)), s
        return convert

    def test_isolates_the_bad_page_and_keeps_the_rest(self):
        failed = []
        md, st = m.convert_resilient(self.make({7}), (1, 10), failed)
        self.assertEqual(failed, [7])  # (retried once, still failing)
        self.assertIn("<!-- pdf2md:failed page=7 -->", md)
        for p in (1, 6, 8, 10):
            self.assertIn(f"page {p}\n", md)
        self.assertEqual(st["eq_total"], 9)  # stats of the 9 good pages survive
        self.assertLess(md.index("page 6"), md.index("pdf2md:failed"))
        self.assertLess(md.index("pdf2md:failed"), md.index("page 8"))

    def test_transient_failure_is_retried_and_cleanup_runs(self):
        calls, cleaned = [], []

        def flaky(rng):
            calls.append(rng)
            if len(calls) == 1:
                raise RuntimeError("CUDA out of memory")
            return "page 1\n", m.new_stats()

        failed = []
        md, _ = m.convert_resilient(flaky, (1, 1), failed, lambda: cleaned.append(1))
        self.assertEqual((md, failed, len(calls), cleaned), ("page 1\n", [], 2, [1]))

    def test_no_failure_is_untouched(self):
        failed = []
        md, st = m.convert_resilient(self.make(set()), (1, 3), failed)
        self.assertEqual((failed, st["eq_total"]), ([], 3))

    def test_unknown_page_count_cannot_split(self):
        with self.assertRaises(RuntimeError):
            m.convert_resilient(self.make({1}), (1, 10**9), [])










class HeadingHygieneTests(unittest.TestCase):
    def test_chapter_pairs_merge(self):
        out = m.fix_headings("## Chapter 3\n\n## Shallow neural networks\n\n## 3.1 Example\n")
        self.assertEqual(out, "# 3 Shallow neural networks\n\n## 3.1 Example\n")
        self.assertEqual(m.fix_headings(out), out)

    def test_code_and_sentences_are_not_headings(self):
        src = ("# Logical AND X = torch.tensor([[0., 0.], [1., 1.]], dtype=torch.float32)\n"
               "# Figure 12.3 Area under a curve is computed by locally approximating the curve segment with tiny lines\n"
               "# Manifolds are second-countable. To explain this, we first outline a few concepts.\n"
               "## 2.1 Real headings stay\n# Plain Title\n")
        out = m.fix_headings(src).split("\n")
        self.assertEqual(out[0], "Logical AND X = torch.tensor([[0., 0.], [1., 1.]], dtype=torch.float32)")
        self.assertTrue(out[1].startswith("Figure 12.3"))
        self.assertTrue(out[2].startswith("Manifolds are"))
        self.assertEqual(out[3:5], ["## 2.1 Real headings stay", "# Plain Title"])

    def test_extraction_artifacts_in_headings(self):
        out = m.fix_headings("text\n\n# \n\n$$\nx\n$$\n\n# > [!note] Figure description (auto-generated)\n> A chart.\n\n# K\n\n## A\n\n# Kernels\n")
        self.assertNotIn("\n# \n", out)
        self.assertIn("\n> [!note] Figure description (auto-generated)\n> A chart.", out)
        self.assertNotIn("# > ", out)
        self.assertIn("\nK\n", out)                    # a lone letter is page furniture, kept as text
        self.assertIn("\nA\n", out)
        self.assertIn("# Kernels", out)                # real headings untouched
        self.assertEqual(m.fix_headings(out), out)

    def test_captions_are_not_headings(self):
        for cap in ("Figure 3.9 Gradient example", "Table 4 Results", "Fig. 2: Overview", "Listing 7.1 Code", "Algorithm 2 Training"):
            self.assertEqual(m.fix_headings(f"## {cap}\n"), f"{cap}\n", cap)
        self.assertEqual(m.fix_headings("## Figures and tables\n"), "## Figures and tables\n")   # ordinary headings stay
        self.assertEqual(m.fix_headings("## Table of contents\n"), "## Table of contents\n")

    def test_toc_entries_that_repeat_a_heading_are_plain_text(self):
        src = "# 2 Vectors and matrices 18\n# 4.5 Singular value decomposition 130\n\n# 2 Vectors and matrices\n\n## 4.5 Singular value decomposition\n"
        out = m.fix_headings(src)
        self.assertTrue(out.startswith("2 Vectors and matrices 18\n4.5 Singular value decomposition 130\n"))
        self.assertIn("# 2 Vectors and matrices\n", out)
        self.assertIn("## 4.5 Singular value decomposition\n", out)

    def test_a_numbered_heading_ending_in_a_number_is_kept_when_it_is_the_only_one(self):
        self.assertEqual(m.fix_headings("## 3.2 The year 2025\n"), "## 3.2 The year 2025\n")


BOOK = """---
cover text

# Contents

# 1 Intro 1
# 2 Models 20

# 1 Intro

Intro text with $x$.

## 1.1 Part

part text

# 2 Models: and more

```python
# 1 fake chapter in code
x = 1
```

Models text. ![[image_1.png]]

# 3 Wrap up

Wrap text.

## Bibliography

refs [1]

## Index

## INDEX

aardvark 3
"""


class SplitTests(unittest.TestCase):
    def test_find_chapters_skips_toc_and_code(self):
        lines = BOOK.split("\n")
        found = m.find_chapters(lines)
        self.assertEqual([(n, t) for _, n, t in found], [(1, "Intro"), (2, "Models: and more"), (3, "Wrap up")])

    def test_split_structure_and_losslessness(self):
        notes = m.split_book(BOOK, "Book")
        self.assertEqual([n["file"] for n in notes],
                         ["00 Front matter", "01 Intro", "02 Models and more", "03 Wrap up", "90 Bibliography", "91 Index"])
        self.assertEqual([n["kind"] for n in notes], ["front", "chapter", "chapter", "chapter", "back", "back"])
        self.assertIn("Intro text with $x$.", notes[1]["body"])
        self.assertIn("## 1.1 Part", notes[1]["body"])
        self.assertIn("# 1 fake chapter in code", notes[2]["body"])        # code stays with its chapter
        self.assertIn("![[image_1.png]]", notes[2]["body"])
        self.assertIn("aardvark 3", notes[5]["body"])                      # repeated INDEX headings: one note
        self.assertNotIn("# 3 Wrap up", notes[3]["body"])                  # the heading becomes the note's own H1
        words = lambda s: sorted(re.findall(r"\S+", s))
        rebuilt = " ".join(n["body"] for n in notes)
        for token in ("cover", "Intro", "part", "Models", "refs", "aardvark", "Wrap", "fake"):
            self.assertIn(token, rebuilt)

    def test_no_chapters_means_no_split(self):
        self.assertIsNone(m.split_book("# Title\n\ntext\n\n## 1.1 x\n", "B"))
        self.assertIsNone(m.split_book("# 1 Only one\n\ntext\n", "B"))
        self.assertIsNone(m.split_book("# 2 Starts at two\n\n# 3 x\n", "B"))     # must start at 1

    def test_names_are_safe_and_unique(self):
        self.assertEqual(m.safe_name('A: b/c? "d" #e [f] ^g'), "A b c d e f g")
        self.assertEqual(m.safe_name("???"), "Untitled")
        self.assertLessEqual(len(m.safe_name("x" * 300)), 80)
        notes = m.split_book("# 1 Same\n\na\n\n# 2 Same\n\nb\n", "B")
        self.assertEqual([n["file"] for n in notes], ["01 Same", "02 Same"])

    def test_render_note_and_index(self):
        notes = m.split_book(BOOK, "Book")
        text = m.render_note(notes[2], "Book", "b.pdf", "01 Intro", "03 Wrap up", "00 Index", "2026-01-01", 0)
        self.assertIn('chapter: 2\n', text)
        self.assertIn("[[01 Intro|Previous]] | [[00 Index|Contents]] | [[03 Wrap up|Next]]", text)
        self.assertIn("# 2 Models: and more\n", text)
        index = m.render_index(notes, "Book", "b.pdf", "2026-01-01")
        self.assertIn("- [[02 Models and more|2. Models: and more]]", index)
        self.assertIn("- [[90 Bibliography|Bibliography]]", index)

    def test_unique_dir(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Book"
            self.assertEqual(m.unique_dir(p), p)
            p.mkdir()
            self.assertEqual(m.unique_dir(p).name, "Book (2)")








class OpenAiBackendTests(unittest.TestCase):
    def capture(self, fn):
        import io
        import json as _json
        from unittest import mock
        sent = {}

        def fake_urlopen(req, timeout=None):
            sent["url"] = req.full_url
            sent["headers"] = {k.lower(): v for k, v in req.header_items()}
            sent["body"] = _json.loads(req.data) if req.data else None
            return io.BytesIO(_json.dumps({"choices": [{"message": {"content": "reply"}}]}).encode())

        with mock.patch.object(m.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(fn(), "reply")
        return sent

    def test_api_base(self):
        self.assertEqual(m.api_base("http://localhost:8080"), "http://localhost:8080/v1")
        self.assertEqual(m.api_base("http://localhost:8080/"), "http://localhost:8080/v1")
        self.assertEqual(m.api_base("http://localhost:8080/v1/"), "http://localhost:8080/v1")
        self.assertEqual(m.api_base("https://openrouter.ai/api/v1"), "https://openrouter.ai/api/v1")

    def test_request_shape(self):
        sent = self.capture(lambda: m.openai_chat("http://h:8080", "mod", "sys", "usr", 5, [b"png"], 700, "KEY"))
        self.assertEqual(sent["url"], "http://h:8080/v1/chat/completions")
        self.assertEqual(sent["headers"]["authorization"], "Bearer KEY")
        b = sent["body"]
        self.assertEqual((b["model"], b["temperature"], b["max_tokens"], b["stream"]), ("mod", 0, 700, False))
        self.assertEqual(b["messages"][0], {"role": "system", "content": "sys"})
        content = b["messages"][1]["content"]
        self.assertEqual([c["type"] for c in content], ["text", "image_url"])     # text first by default
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))



class PageLinkAndDoubtTests(unittest.TestCase):
    def test_link_target(self):
        self.assertEqual(m.link_target("Book.pdf", 12), "[[Book.pdf#page=12|p. 12]]")
        self.assertEqual(m.link_target("A Big Book (2020).pdf", 3), "[[A Big Book (2020).pdf#page=3|p. 3]]")
        for bad in ("a[b].pdf", "a|b.pdf", "a#b.pdf", "a^b.pdf", ""):
            self.assertIsNone(m.link_target(bad, 1), bad)

    def test_which_headings_get_links(self):
        self.assertTrue(m.wants_page_link("2.1 Supervised learning", 2))
        self.assertTrue(m.wants_page_link("Introduction", None))
        self.assertFalse(m.wants_page_link("Chapter 7", 1))                   # merged with its title later
        self.assertFalse(m.wants_page_link("Figure 3.9 A diagram", 2))
        self.assertFalse(m.wants_page_link("2 Vectors and matrices 18", 1))   # table-of-contents line
        self.assertFalse(m.wants_page_link("x = torch.tensor([1, 2])", 1))     # code comment
        self.assertFalse(m.wants_page_link("A deep subsection", 4))           # too deep
        self.assertFalse(m.wants_page_link("K", 2))







class PaddleMarkdownTests(unittest.TestCase):
    TABLE = ("<table border=1 style='margin: auto;'><tr><td style='text-align: center;'>Type</td><td>Fall</td></tr>"
             "<tr><td>Domestic | US</td><td>Jan<br>15</td></tr></table>")

    def test_simple_table_becomes_markdown(self):
        out = m.html_table_to_md(self.TABLE)
        self.assertEqual(out.split("\n"), ["| Type | Fall |", "| --- | --- |", "| Domestic \\| US | Jan<br>15 |"])

    def test_merged_cells_are_left_as_html_and_ragged_rows_padded(self):
        self.assertIsNone(m.html_table_to_md("<table><tr><td colspan='2'>A</td></tr><tr><td>b</td><td>c</td></tr></table>"))
        self.assertIsNone(m.html_table_to_md("<table><tr><td rowspan=\"2\">A</td><td>x</td></tr></table>"))
        self.assertIsNone(m.html_table_to_md("<p>no table</p>"))
        ragged = m.html_table_to_md("<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>")
        self.assertEqual(ragged.split("\n")[-1], "| c |  |")
        self.assertIsNotNone(m.html_table_to_md("<table><tr><td colspan='1'>a</td></tr></table>"))   # colspan=1 is not merged

    def test_page_markdown_is_cleaned(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "page_0011"
            (src / "imgs").mkdir(parents=True)
            (src / "imgs" / "box_1.jpg").write_bytes(b"jpg")
            md = ('<div style="text-align: center;"><img src="imgs/box_1.jpg" alt="Image" width="54%" /></div>\n\n'
                  '<div style="text-align: center;">FIGURE 12. Two  $ \\sharp $-moves convert  $ T $.</div>\n\n'
                  '## 4.3 Slice genus\n\nA  sentence  with gaps.\n\n' + self.TABLE + '\n\n<img src="imgs/missing.jpg" />\n')
            out = m.clean_paddle_markdown(md, 11, src, Path(d) / "assets", "[[B.pdf#page=11|p. 11]]")
            self.assertIn("![[p0011_box_1.jpg]]", out)
            self.assertEqual((Path(d) / "assets" / "p0011_box_1.jpg").read_bytes(), b"jpg")
            self.assertNotIn("<div", out)
            self.assertNotIn("<img", out)                                   # the unresolvable one is dropped
            self.assertIn("FIGURE 12. Two $ \\sharp $-moves convert $ T $.", out)   # spacing collapsed, maths untouched
            self.assertIn("## 4.3 Slice genus\n\n[[B.pdf#page=11|p. 11]]\n\nA sentence with gaps.", out)
            self.assertIn("| Type | Fall |", out)
            self.assertNotIn("<table", out)

    def test_tables_code_and_unlinkable_headings_are_left_alone(self):
        out = m.clean_paddle_markdown("# Title\n\n```\na   b\n```\n\n| a   b | c |\n| --- | --- |\n\n## Figure 3 caption", 2, None, None,
                                      "[[B.pdf#page=2|p. 2]]")
        self.assertIn("a   b\n```", out)                                   # code keeps its spaces
        self.assertIn("| a   b | c |", out)                                 # so do table rows
        self.assertEqual(out.count("[[B.pdf#page=2|p. 2]]"), 1)             # "Figure 3 caption" is not a heading
        self.assertEqual(m.clean_paddle_markdown("# T\n", 1, None, None, None), "# T\n")   # no link when none given

    def test_paddle_output_flows_through_the_math_pipeline(self):
        raw = ("Consider $ \\sigma(K)=0 $ and\n\n $$ \\sigma(K)+4\\cdot A r f(K)\\equiv0~{o r}~\\pm2 $$ \n\n $$ G_{F}(K)=\\begin{bmatrix}l&m\\\\ m&n\\end{bmatrix} $$ \n")
        final = m.normalize_math(m.clean_paddle_markdown(raw, 1, None, None))
        self.assertIn("Consider $\\sigma(K)=0$ and", final)                # inline spaces trimmed
        self.assertIn("$$\nG_{F}(K)=\\begin{bmatrix}l&m\\\\ m&n\\end{bmatrix}\n$$", final)
        self.assertIn("Arf(K)", final)                                      # letter-spaced words rejoined in display maths only
        self.assertEqual(m.validate_document(final), [])


class ServerGuardTests(unittest.TestCase):
    def serve(self):
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def free_port(self):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_a_running_server_is_used_and_never_started_or_stopped(self):
        url = self.serve()
        with m.ServerGuard(url, "definitely-not-a-command", 5) as g:
            self.assertIsNone(g.proc)
        self.assertTrue(g.alive())                                          # still up after the guard

    def test_missing_server_without_a_command_explains_how_to_start_one(self):
        with self.assertRaises(SystemExit) as cm:
            m.ServerGuard(f"http://127.0.0.1:{self.free_port()}", None, 5).__enter__()
        self.assertIn("llama-server", str(cm.exception.code))
        self.assertIn("--serve-command", str(cm.exception.code))

    def test_command_starts_the_server_and_is_stopped_afterwards(self):
        port = self.free_port()
        code = ("import http.server\n"
                "class H(http.server.BaseHTTPRequestHandler):\n"
                "    def do_GET(self):\n        self.send_response(200); self.end_headers(); self.wfile.write(b'{}')\n"
                "    def log_message(self, *a): pass\n"
                f"http.server.HTTPServer(('127.0.0.1', {port}), H).serve_forever()\n")
        cmd = f"{sys.executable} -c \"{code}\""
        import shlex
        guard = m.ServerGuard(f"http://127.0.0.1:{port}", " ".join(shlex.quote(a) for a in [sys.executable, "-c", code]), 30)
        with guard:
            self.assertIsNotNone(guard.proc)
            self.assertTrue(guard.alive())
            pid = guard.proc.pid
        self.assertIsNone(guard.proc)
        import time
        for _ in range(50):
            if not guard.alive():
                break
            time.sleep(0.1)
        self.assertFalse(guard.alive())                                     # we stopped what we started

    def test_a_command_that_dies_or_never_answers_is_reported(self):
        with self.assertRaises(SystemExit) as cm:
            m.ServerGuard(f"http://127.0.0.1:{self.free_port()}", f"{sys.executable} -c \"import sys; sys.exit(3)\"", 10).__enter__()
        self.assertIn("exited (code 3)", str(cm.exception.code))
        with self.assertRaises(SystemExit) as cm:
            m.ServerGuard(f"http://127.0.0.1:{self.free_port()}", f"{sys.executable} -c \"import time; time.sleep(30)\"", 2).__enter__()
        self.assertIn("did not become ready", str(cm.exception.code))


class PaddleSliceTests(unittest.TestCase):
    def fake_worker(self, d, body):
        f = Path(d) / "fake_worker.py"
        f.write_text(body)
        return lambda pdf, rng, server, out, scale=2.5: [sys.executable, str(f), str(out), str(rng[0]), str(rng[1]), server]

    WORKER = (
        "import sys\nfrom pathlib import Path\n"
        "out, first, last = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])\n"
        "for p in range(first, last + 1):\n"
        "    if p == 3:\n        print('ERROR page 3: boom', flush=True); continue\n"
        "    d = out / f'page_{p:04d}'; (d / 'imgs').mkdir(parents=True)\n"
        "    (d / 'imgs' / 'a.jpg').write_bytes(b'x')\n"
        "    (d / 'doc.md').write_text(f'# Part {chr(64 + p)}\\n\\nText on page {p}. $ x $\\n\\n<img src=\"imgs/a.jpg\" />\\n\\n$$ y $$\\n')\n"
        "    print(f'PAGE {p - first + 1}/{last - first + 1}', flush=True)\n")

    def test_every_page_is_reported_as_it_finishes(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            seen = []
            with mock.patch.object(m, "worker_command", self.fake_worker(d, self.WORKER)):
                m.paddle_slice(Path(d) / "b.pdf", (1, 5), Path(d) / "assets", "http://s/v1", False,
                               lambda n, total: seen.append((n, total)))
            self.assertEqual(seen, [(1, 5), (2, 5), (4, 5), (5, 5)])           # page 3 failed, so no tick

    def test_pages_are_joined_cleaned_and_bad_pages_marked(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(m, "worker_command", self.fake_worker(d, self.WORKER)):
                md, st = m.paddle_slice(Path(d) / "My Book.pdf", (1, 4), Path(d) / "assets", "http://s/v1", True)
            self.assertLess(md.index("Text on page 1"), md.index("Text on page 2"))
            self.assertLess(md.index("Text on page 2"), md.index("Text on page 4"))
            self.assertIn("<!-- pdf2md:failed page=3 -->", md)
            self.assertNotIn("Text on page 3", md)
            self.assertIn("# Part B\n\n[[My Book.pdf#page=2|p. 2]]", md)
            self.assertEqual(sorted(p.name for p in (Path(d) / "assets").iterdir()), ["p0001_a.jpg", "p0002_a.jpg", "p0004_a.jpg"])
            self.assertIn("![[p0004_a.jpg]]", md)
            self.assertEqual(st["eq_total"], 3)

    def test_no_links_when_not_wanted_and_total_failure_raises(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(m, "worker_command", self.fake_worker(d, self.WORKER)):
                md, _ = m.paddle_slice(Path(d) / "b.pdf", (1, 2), Path(d) / "assets", "http://s/v1", False)
            self.assertNotIn("#page=", md)
            dead = "import sys; sys.exit(7)\n"
            with mock.patch.object(m, "worker_command", self.fake_worker(d, dead)):
                with self.assertRaises(RuntimeError):
                    m.paddle_slice(Path(d) / "b.pdf", (1, 2), Path(d) / "assets2", "http://s/v1", False)

    def test_worker_command_uses_the_bundled_script(self):
        cmd = m.worker_command(Path("/x/a.pdf"), (5, 9), "http://s/v1", Path("/o"))
        self.assertTrue(Path(cmd[0]).parts[-3:-1] == ("worker-venv", "bin") or cmd[0].endswith("python.exe"))
        self.assertTrue(cmd[1].endswith("paddle_worker.py") and Path(cmd[1]).exists())
        self.assertEqual(cmd[cmd.index("--first") + 1:cmd.index("--first") + 4], ["5", "--last", "9"])
        self.assertEqual(cmd[cmd.index("--scale") + 1], "2.5")
        high = m.worker_command(Path("/x/a.pdf"), (1, 2), "http://s/v1", Path("/o"), 4.0)
        self.assertEqual(high[high.index("--scale") + 1], "4.0")




class LightMathTests(unittest.TestCase):
    def test_typography_becomes_plain_characters(self):
        f = m.simplify_inline_math
        self.assertEqual(f(" CO_{2} "), "CO\u2082")
        self.assertEqual(f("CO_2"), "CO\u2082")
        self.assertEqual(f("NO_{x}"), "NO\u2093")
        self.assertEqual(f("H_{2}O"), "H\u2082O")
        self.assertEqual(f("N_2"), "N\u2082")
        self.assertEqual(f(" _{2} "), "\u2082")
        self.assertEqual(f("^{rd}"), "\u02b3\u1d48")
        self.assertEqual(f("10^{-3}"), "10\u207b\u00b3")
        self.assertEqual(f(r" \pm "), "\u00b1")
        self.assertEqual(f(r"\leq 5"), "\u2264 5")
        self.assertEqual(f(r"\mu"), "\u00b5")

    def test_ordinary_maths_is_left_alone(self):
        f = m.simplify_inline_math
        for keep in ("K_k", "H_{k}", "R_{n}", "x_i", "B^4", "I_{CaL}", "CO_{abc}", "a^2 + b^2", r"\frac{1}{2}", "x", "10_2",
                     "^{*}", r"\alpha_{1}", "CO^2", r"\unknown", "e^{x}", "H_2 O_2 = 3"):
            self.assertIsNone(f(keep), keep)

    def test_applied_to_spans_outside_code_and_display_maths(self):
        md = "Burning $ CO_{2} $ and $K_k$ on the 3 $ ^{rd} $ day, $ \\pm $ 5.\n\n$$ CO_{2} $$\n\n```\n$CO_2$\n```\n"
        out = m.simplify_light_math(md)
        self.assertIn("Burning CO\u2082 and $K_k$ on the 3\u02b3\u1d48 day, \u00b1 5.", out)
        self.assertIn("$$ CO_{2} $$", out)
        self.assertIn("```\n$CO_2$\n```", out)
        self.assertEqual(m.simplify_light_math(out), out)                      # idempotent

    def test_flows_through_the_cleaner_including_table_cells(self):
        out = m.clean_paddle_markdown("<table><tr><td>[SO $ _{2} $]</td><td>[NO $ _{x} $]</td></tr></table>\n\nBurns  $ CO_{2} $.", 1, None, None)
        self.assertIn("| [SO\u2082] | [NO\u2093] |", out)
        self.assertIn("Burns CO\u2082.", out)


class TextMacroTests(unittest.TestCase):
    def test_units_and_symbols_in_text(self):
        f = m.text_macros_to_unicode
        self.assertEqual(f(r"250 \mu M and 0 \mu M"), "250 \u00b5M and 0 \u00b5M")      # TeX eats the space after a macro
        self.assertEqual(f(r"\pm 5 and 3 \times 10 and x \le 5, y \geq 2"), "\u00b1 5 and 3 \u00d7 10 and x \u2264 5, y \u2265 2")
        self.assertEqual(f(r"\alpha-helix, \beta sheet, \Delta T"), "\u03b1-helix, \u03b2 sheet, \u0394 T")
        self.assertEqual(f(r"50\% of A\&B, a\_b"), "50% of A&B, a_b")
        self.assertEqual(f(r"20 \degree C"), "20 \u00b0 C")

    def test_math_code_and_longer_macros_are_left_alone(self):
        f = m.text_macros_to_unicode
        for keep in (r"$250 \mu M$", r"$$ a \le b $$", r"a \leftarrow b", r"\midpoint", r"\leq5".replace("\\leq5", "\\lex")):
            self.assertEqual(f(keep), keep, keep)
        self.assertEqual(f("a \\mu b $x \\mu$ c \\le d"), "a \u00b5b $x \\mu$ c \u2264 d")   # text around a span is converted, the span is not
        self.assertEqual(f("```\n\\mu\n```\n\\mu"), "```\n\\mu\n```\n\u00b5")

    def test_table_cells_and_the_full_cleaner(self):
        html = "<table><tr><td>[CO]</td><td>[NO $ _{x} $]</td></tr><tr><td>250 \\mu M</td><td>0.005 nM</td></tr></table>"
        out = m.clean_paddle_markdown(html, 1, None, None)
        self.assertIn("| 250 \u00b5M | 0.005 nM |", out)
        self.assertIn("[NO\u2093]", out)                                      # typographic maths in a cell becomes text


if __name__ == "__main__":
    unittest.main()


