"""Tests for pdf_to_md (no Docling or Ollama needed)."""
import importlib.util
import re
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "pdf_to_md", Path(__file__).resolve().parents[1] / "pdf_to_md.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


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
        self.assertEqual(m.repair_latex(r"\left( a \leftarrow b"), r"( a \leftarrow b")   # repair keeps the arrow

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


class ProtectTests(unittest.TestCase):
    SRC = ("Intro $x$ text.\n\n$$\na=b\n$$\n\n| a | $y$ |\n|---|---|\n| 1 | 2 |\n\n"
           "![[img.png]]\n\n> [!note] Figure description (auto-generated)\n> A chart.\n\n"
           "<!-- note -->\n\n```\ncode $z$\n```\n")

    def test_round_trip_and_masking(self):
        masked, store = m.protect(self.SRC)
        self.assertEqual(m.restore(masked, store), self.SRC)
        for s in ["$x$", "a=b", "| 1 | 2 |", "img.png", "A chart.", "note", "code"]:
            self.assertNotIn(s, masked)

    def test_chunks_never_split_math_or_code(self):
        src = "p1\n\n$$\na\n\nb\n$$\n\n```\nx\n\ny\n```\n\n" + "word " * 50 + "\n\np3\n"
        for c in m.split_chunks(src, 20):
            self.assertEqual(c.count("$$") % 2, 0)
            self.assertEqual(c.count("```") % 2, 0)
        self.assertEqual("".join(m.split_chunks(src, 20)), src)  # lossless


class CleanChunkTests(unittest.TestCase):
    CHUNK = ("Para one is a long sentence with enough words to be worth cleaning up. $x$\n\n"
             "Para two is another long sentence with enough words to be worth cleaning up.\n\n"
             "$$\na=b\n$$\n\n" + "\n\n".join(f"Padding paragraph {i} goes on and on for a while, with words. " * 3 for i in range(8)) + "\n\n"
             "Para four is a long sentence with plenty of words to be cleaned too. $y$\n")

    def test_good_reply_is_used_and_math_restored(self):
        out, bad = m.clean_chunk(self.CHUNK, lambda t: t.replace("Para", "Section"), [])
        self.assertEqual(bad, 0)
        self.assertIn("Section one", out)
        self.assertIn("$$\na=b\n$$", out)
        self.assertIn("$x$", out)

    def test_dropped_placeholder_falls_back_after_splitting(self):
        calls, log = [], []

        def chat(t):
            calls.append(t)
            return "I dropped everything."

        out, bad = m.clean_chunk(self.CHUNK, chat, log)
        self.assertEqual(out, self.CHUNK)
        self.assertGreater(bad, 0)
        self.assertGreater(len(calls), 2)  # two full attempts, then the halves
        self.assertTrue(any("placeholders changed" in l for l in log))

    def test_split_recovers_when_only_big_input_fails(self):
        full = len(m.protect(self.CHUNK)[0])

        def chat(t):
            if len(t) > full * 0.9:
                raise urllib.error.URLError("boom")
            return t.replace("Para", "Section")

        out, bad = m.clean_chunk(self.CHUNK, chat, [])
        self.assertEqual(bad, 0)
        self.assertIn("Section", out)
        self.assertIn("$$\na=b\n$$", out)

    def test_length_ratio_rejected(self):
        self.assertIn("length ratio", m.check_output("x" * 100, "x" * 10))
        self.assertIsNone(m.check_output("x" * 100, "x" * 100))

    def test_invented_scripts_rejected(self):
        masked = "Źŵ ƾŤƃřŶƸŝ ƶŝ " * 10
        self.assertIn("introduced characters", m.check_output(masked, masked.replace("Źŵ", "づぉャ")))
        self.assertIsNone(m.check_output("日本語 text " * 10, "日本語 text " * 10))   # present in the input: fine

    def test_tiny_chunks_skip_the_model(self):
        out, bad = m.clean_chunk("$$\na\n$$\n", lambda t: 1 / 0, [])
        self.assertEqual((out, bad), ("$$\na\n$$\n", 0))


class VisionTests(unittest.TestCase):
    def test_valid_text_does_not_call_the_model(self):
        def boom(_):
            raise AssertionError("model called")

        self.assertEqual(m.repair_formula("x ^ { 2 }", True, lambda: b"png", boom), ("x^{2}", "tidied"))
        self.assertEqual(m.repair_formula("x^{2}", True, lambda: b"png", boom), ("x^{2}", "kept"))

    def test_empty_formula_is_reread(self):
        out = m.repair_formula("", True, lambda: b"png", lambda png: "```latex\n$$ a ^ { 2 } $$\n```")
        self.assertEqual(out, ("a^{2}", "repaired"))

    def test_invalid_reply_is_rejected(self):
        out = m.repair_formula("\\frac{a", True, lambda: b"png", lambda png: "\\frac{b")
        self.assertEqual(out[1], "unresolved")

    def test_no_vision_leaves_unresolved(self):
        self.assertEqual(m.repair_formula("", True, lambda: b"png", None)[1], "unresolved")

    def test_model_error_is_unresolved(self):
        def ask(_):
            raise urllib.error.URLError("down")

        self.assertEqual(m.repair_formula("\\frac{a", True, lambda: b"png", ask)[1], "unresolved")

    def test_figure_description_filter(self):
        self.assertEqual(m.usable_description("SKIP"), "")
        self.assertEqual(m.usable_description("  skip.\n"), "")
        self.assertEqual(m.usable_description(""), "")
        self.assertEqual(m.usable_description("A bar chart\nof sales."), "A bar chart of sales.")
        self.assertIn("SKIP", m.FIGURE_SYSTEM)

    def test_strip_reply(self):
        self.assertEqual(m.strip_latex_reply("$$\nx\n$$"), "x")
        self.assertEqual(m.strip_latex_reply("\\[x\\]"), "x")


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


class ParallelTests(unittest.TestCase):
    def test_page_ranges(self):
        self.assertEqual(m.page_ranges(None, 40), [(1, 10**9)])
        self.assertEqual(m.page_ranges(30, 40), [(1, 30)])
        self.assertEqual(m.page_ranges(100, 40), [(1, 40), (41, 80), (81, 100)])
        self.assertEqual(m.page_ranges(80, 40), [(1, 40), (41, 80)])
        self.assertEqual(m.page_ranges(500, 0), [(1, 500)])
        covered = [p for lo, hi in m.page_ranges(123, 25) for p in range(lo, hi + 1)]
        self.assertEqual(covered, list(range(1, 124)))  # no gaps, no overlaps

    def test_page_subset(self):
        self.assertEqual(m.page_ranges(140, 20, 100), [(100, 119), (120, 139), (140, 140)])
        self.assertEqual(m.page_ranges(60, 40, 50), [(50, 60)])
        self.assertEqual(m.parse_pages(None, 552), (1, 552))
        self.assertEqual(m.parse_pages("60-79", 552), (60, 79))
        self.assertEqual(m.parse_pages(" 7 ", 552), (7, 7))
        self.assertEqual(m.parse_pages("500-900", 552), (500, 552))
        for bad in ("x", "9-3", "0-4", "600-700"):
            with self.assertRaises(SystemExit):
                m.parse_pages(bad, 552)

    def test_formula_ok(self):
        self.assertTrue(m.formula_ok("x ^ { 2 }"))
        self.assertFalse(m.formula_ok(""))
        self.assertFalse(m.formula_ok("\\frac{a"))

    def test_parallel_cleaning_keeps_document_order_and_isolates_failures(self):
        import random
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor

        lock, live, peak = threading.Lock(), [0], [0]
        text = "".join(f"Paragraph {i} " + "words go here and there. " * 6 + "\n\n" for i in range(12))
        chunks = m.split_chunks(text, 150)
        self.assertGreater(len(chunks), 6)

        def chat(masked):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(random.random() * 0.03)
            with lock:
                live[0] -= 1
            if "Paragraph 3 " in masked:
                return "garbage"          # this chunk must fall back, others succeed
            return masked.replace("words", "WORDS")

        with ThreadPoolExecutor(4) as pool:
            futs = [pool.submit(m.clean_chunk_logged, c, chat) for c in chunks]
            results = [f.result() for f in futs]
        self.assertGreater(peak[0], 1)    # really ran concurrently
        joined = "".join(r[0] for r in results)
        self.assertEqual(joined.count("Paragraph"), 12)
        self.assertEqual([i for i in range(12) if f"Paragraph {i} " in joined], list(range(12)))
        self.assertTrue(any(r[1] for r in results))
        self.assertIn("WORDS", joined)
        bad = [r for r in results if r[1]]
        self.assertTrue(all("Paragraph 3 " in r[0] and "WORDS" not in r[0] for r in bad))


class SelectiveCleaningTests(unittest.TestCase):
    CLEAN = "A perfectly ordinary paragraph that Docling extracted without any damage at all.\n\n## Heading\n"

    def test_clean_text_is_left_alone(self):
        self.assertEqual(m.damage_reasons(self.CLEAN), [])
        self.assertFalse(m.chunk_needs_cleaning(self.CLEAN, "auto"))

    def test_damage_signals(self):
        cases = {"ligature": "the e\ufb03cient method", "hyphen": "compu-\ntation of values",
                 "soft wrap": "the model is trained on\nlarge data", "page number": "text\n\n 123 \n\ntext",
                 "double spaces": "words  with  gaps", "repeated": "Chapter 3 Running Header\ntext\nChapter 3 Running Header"}
        for name, text in cases.items():
            self.assertTrue(m.damage_reasons(text), name)

    def test_lost_glyphs_are_not_cleanable(self):
        self.assertEqual(m.damage_reasons("vectors \ufffd a and \ufffd b"), [])  # vision handles these, not the cleaner

    def test_modes(self):
        dirty = "the e\ufb03cient method"
        self.assertTrue(m.chunk_needs_cleaning(dirty, "auto"))
        self.assertTrue(m.chunk_needs_cleaning(self.CLEAN, "all"))
        self.assertFalse(m.chunk_needs_cleaning(dirty, "none"))

    def test_math_and_tables_do_not_count_as_damage(self):
        chunk = "Intro text here.\n\n$$\na  =  b\n$$\n\n| a  |  b |\n|---|---|\n| 1  | 2 |\n"
        self.assertFalse(m.chunk_needs_cleaning(chunk, "auto"))


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


class InlineMathTests(unittest.TestCase):
    ORIG = "Clark showed that C(K) \u2264 2 \u22c5 g(K) + 1 for any knot K and asked if this bound was achieved."

    def test_looks_mathy(self):
        self.assertTrue(m.looks_mathy(self.ORIG))
        self.assertTrue(m.looks_mathy("Hence the loss is L = f(x) over the whole training set."))
        self.assertTrue(m.looks_mathy("The learning rate \u03b1 controls the step size taken."))
        self.assertFalse(m.looks_mathy("Plain prose with no symbols at all, just words and commas."))
        self.assertFalse(m.looks_mathy("short"))
        self.assertFalse(m.looks_mathy("Already $x^2$ is converted \u2264 so skip."))

    def test_new_hints_and_modes(self):
        self.assertTrue(m.looks_mathy("Let X be a cube complex and let Ha, Hb, Hc be hyperplanes of the complex."))
        self.assertTrue(m.looks_mathy("Both collapses cab : X shortrightarrow Sab are homotopy equivalences here."))
        self.assertFalse(m.looks_mathy("In it we do as he said, so we go on to the next part of it."))
        plain = "The transmitter is equipped with Nt antennas, each of the same size."
        self.assertFalse(m.looks_mathy(plain, "auto"))
        self.assertTrue(m.looks_mathy(plain, "all"))
        self.assertFalse(m.looks_mathy("$x$ already converted paragraph text here", "all"))
        self.assertFalse(m.looks_mathy("1 2 3 4 5 6 7 8 9 10 11 12", "all"))

    def test_acronyms_are_unwrapped_but_variables_stay(self):
        out = m.finalize_inline("A $FAs$ system with $IDET$ and $N_t$ and $x$, $AB$ and $GPU$.")
        self.assertEqual(out, "A FAs system with IDET and $N_t$ and $x$, $AB$ and GPU.")

    def test_emphasised_words_are_not_math(self):
        out = m.finalize_inline("A $unit$ $vector$ has length $1$ and $\\vec{v}$, $abc$ and $loss$.")
        self.assertEqual(out, "A unit vector has length $1$ and $\\vec{v}$, $abc$ and loss.")

    def test_left_right_repair(self):
        orig = "Let the value f(x) \u2264 g(x) hold for the given function."
        new = "Let the value $\\left( f(x) \\le g(x)$ hold for the given function."
        self.assertTrue(m.accept_inline(orig, new))
        self.assertNotIn("\\left", m.finalize_inline(new))
        self.assertEqual(m.repair_latex("\\left. x + 1"), " x + 1")
        self.assertEqual(m.repair_latex("\\left( x + 1"), "( x + 1")
        self.assertEqual(m.repair_latex("\\left( x \\right)"), "\\left( x \\right)")
        self.assertEqual(m.repair_latex("a^2"), "a^2")

    def test_accepts_faithful_rewrite(self):
        new = "Clark showed that $C(K) \\le 2 \\cdot g(K) + 1$ for any knot $K$ and asked if this bound was achieved."
        self.assertTrue(m.accept_inline(self.ORIG, new))

    def test_rejects_bad_rewrites(self):
        good = "Clark showed that $C(K) \\le 2$ for any knot and asked if this bound was achieved."
        self.assertTrue(m.accept_inline(self.ORIG, good))
        bad = {
            "no math": "Clark showed that C(K) is at most something for any knot and asked if this bound was achieved.",
            "display": "Clark showed $$C(K)$$ for any knot and asked if this bound was achieved.",
            "odd dollar": "Clark showed that $C(K) for any knot and asked if this bound was achieved.",
            "invalid latex": "Clark showed that $\\frac{C(K$ for any knot and asked if this bound was achieved.",
            "dropped words": "$C(K)$ for any knot.",
            "invented words": "Clark showed that $C(K)$ for any knot and then provided an entirely different explanation here.",
            "empty": "",
        }
        for name, text in bad.items():
            self.assertFalse(m.accept_inline(self.ORIG, text), name)


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

    def test_cached_clean_calls_model_once(self):
        calls = []

        def chat(t):
            calls.append(t)
            return t.replace("alpha", "ALPHA")

        chunk = "Some alpha text that is long enough to be worth sending to the model for cleaning.\n"
        with tempfile.TemporaryDirectory() as d:
            c = m.Cache(Path(d))
            first = m.cached_clean(c, chunk, chat, "model-a")
            second = m.cached_clean(c, chunk, chat, "model-a")
            self.assertEqual(first, second)
            self.assertEqual(len(calls), 1)
            m.cached_clean(c, chunk, chat, "model-b")   # different model = different entry
            self.assertEqual(len(calls), 2)

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
        self.assertIn("page 7 could not be converted (RuntimeError)", md)
        for p in (1, 6, 8, 10):
            self.assertIn(f"page {p}\n", md)
        self.assertEqual(st["eq_total"], 9)  # stats of the 9 good pages survive
        self.assertLess(md.index("page 6"), md.index("could not be converted"))
        self.assertLess(md.index("could not be converted"), md.index("page 8"))

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


class LostGlyphAndProseFormulaTests(unittest.TestCase):
    def test_replacement_characters_route_to_vision(self):
        self.assertTrue(m.looks_mathy("vectors \ufffd a and \ufffd b can be viewed as points"))
        self.assertTrue(m.looks_mathy("y = X \ufffdw + b."))          # short, still math
        self.assertTrue(m.looks_mathy("the arrow \ue0a1 glyph here"))
        self.assertFalse(m.looks_mathy("\ufffd"))                      # too short to be a paragraph
        self.assertFalse(m.looks_mathy("already $x$ \ufffd done"))

    def test_prose_formula_detection(self):
        prose = (r"\text{section 2.5.3, given a model weight vector, say \vec{w} = \left [ \begin{matrix} 3 \\ 2 "
                 r"\end{matrix} \right ], and the bias value b = 5, the} \\ [ 0.11 ]")
        self.assertTrue(m.is_prose_formula(prose))
        self.assertFalse(m.is_prose_formula(r"\frac{a}{b} = \text{where } x"))
        self.assertFalse(m.is_prose_formula(r"\text{softmax}(x)_i = \frac{e^{x_i}}{\sum_j e^{x_j}}"))
        hint = m.prose_hint(prose)
        self.assertIn("given a model weight vector", hint)
        self.assertNotIn("\\text", hint)

    def test_swap_never_eats_neighbouring_display_math(self):
        md = ("$$a=b$$\n\nPDFMDINLINE0X\n\n$$c=d$$\n\n$$PDFMDINLINE1X$$\n\nend $$e=f$$\n")
        out = m.apply_swaps(md, {"PDFMDINLINE0X": "A paragraph with $x$.", "PDFMDINLINE1X": "A prose formula with $y$."})
        self.assertIn("$$a=b$$\n\nA paragraph with $x$.\n\n$$c=d$$", out)   # neighbours untouched
        self.assertIn("A prose formula with $y$.", out)
        self.assertNotIn("$$PDFMDINLINE", out)
        self.assertEqual(out.count("$$"), 6)         # three equations x two delimiters; the prose wrapper is gone
        self.assertEqual(m.validate_document(m.normalize_math(out)), [])

    def test_dense_chunk_is_split_before_calling_the_model(self):
        para = "Sentence number %d has $a_%d$ and plenty of words around it to be worth cleaning up properly."
        chunk = "\n\n".join(para % (i, i) for i in range(60)) + "\n"
        sizes = []

        def chat(masked):
            sizes.append(len(m.TOKEN_RE.findall(masked)))
            return masked

        out, bad = m.clean_chunk(chunk, chat, [])
        self.assertEqual(bad, 0)
        self.assertGreater(len(sizes), 1)
        self.assertTrue(all(n <= m.MAX_PROTECTED for n in sizes), sizes)
        self.assertEqual(out.count("$a_"), 120 // 2)

    def test_failure_cascade_is_bounded(self):
        calls = []
        chunk = "\n\n".join(f"Paragraph {i} " + "text words go on here. " * 8 + " $x$" for i in range(20)) + "\n"

        def bad_chat(masked):
            calls.append(1)
            return "nothing useful"

        out, bad = m.clean_chunk(chunk, bad_chat, [])
        self.assertEqual(out, chunk)
        self.assertLessEqual(len(calls), 2 + 3)       # two full attempts, then one per piece; no deeper cascade


class MainSmokeTests(unittest.TestCase):
    """Run main() end to end with Docling and Ollama replaced, so wiring mistakes surface."""

    def run_main(self, argv, convert_md="Intro text.\n\n$$\nE = mc^2\n$$\n"):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4 fake")
            out = Path(d) / "out"
            seen = []

            def fake_convert(conv, pdf_, rng, assets, pool, *rest):
                seen.append(rng)
                (assets).mkdir(parents=True, exist_ok=True)
                (assets / "image_000000_abc.png").write_bytes(b"png")
                return convert_md, m.new_stats()

            with mock.patch.object(m, "make_converter", lambda *a, **k: object()), \
                 mock.patch.object(m, "convert_slice", fake_convert), \
                 mock.patch.object(m, "page_count", lambda p: 100), \
                 mock.patch.object(m, "check_ollama", lambda *a, **k: None), \
                 mock.patch.object(m, "CACHE_ROOT", Path(d) / "cache"), \
                 mock.patch.object(sys, "argv", ["pdf_to_md", str(pdf), "-o", str(out), "--no-backup", *argv]):
                m.main()
            md = next(out.glob("*.md")).read_text()
            return md, seen, sorted(p.name for p in out.glob("*_assets/*")), list((Path(d) / "cache").glob("*"))

    def test_whole_pipeline_wires_up(self):
        md, seen, assets, cache_left = self.run_main(["--no-llm", "--pages", "10-60", "--pages-per-batch", "20"])
        self.assertEqual(seen, [(10, 29), (30, 49), (50, 60)])
        self.assertIn("E = mc^2", md)
        self.assertIn('pages: "10-60"', md)
        self.assertEqual(assets, ["image_000000_abc.png"])
        self.assertEqual(cache_left, [])                 # checkpoint removed after success

    def test_math_heading_and_entity_fixes_applied(self):
        md, *_ = self.run_main(["--no-llm"], convert_md="# 3.4 Title &amp; more\n\nSome \\(x\\) text.\n")
        self.assertIn("## 3.4 Title & more", md)
        self.assertIn("$x$", md)


class OllamaRequestTests(unittest.TestCase):
    def capture(self, **kw):
        import io
        import json as _json
        from unittest import mock
        sent = {}

        def fake_urlopen(req, timeout=None):
            sent.update(_json.loads(req.data))
            return io.BytesIO(_json.dumps({"message": {"content": "ok"}}).encode())

        with mock.patch.object(m.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(m.ollama_chat("http://h", "mod", "sys", "usr", 5, **kw), "ok")
        return sent

    def test_output_is_capped_and_context_fixed(self):
        sent = self.capture(max_out=900, images=[b"png"])
        self.assertEqual(sent["options"], {"temperature": 0, "num_ctx": m.NUM_CTX, "num_predict": 900})
        self.assertEqual(sent["think"], False)
        self.assertEqual(len(sent["messages"][1]["images"]), 1)

    def test_no_cap_means_no_num_predict(self):
        self.assertNotIn("num_predict", self.capture()["options"])

    def test_caps_cover_every_request_kind(self):
        self.assertEqual(set(m.MAX_OUT), {"latex", "inline", "figure", "clean"})
        self.assertTrue(all(v >= 200 for v in m.MAX_OUT.values()))


class LastResortFormulaTests(unittest.TestCase):
    def test_prose_note_becomes_a_paragraph(self):
        with tempfile.TemporaryDirectory() as d:
            kind, out = m.last_resort_formula("\\hat{v} garbage Unitvertos", b"png",
                                              lambda png, hint: "NOTE Unit vectors are written with a hat, as in $\\hat{u}$.", Path(d))
            self.assertEqual(kind, "paragraph")
            self.assertIn("$\\hat{u}$", out)
            self.assertEqual(list(Path(d).iterdir()), [])        # nothing written to assets

    def test_unreadable_formula_is_embedded_as_a_picture(self):
        with tempfile.TemporaryDirectory() as d:
            for reply in ("$\\frac{a$", "", "$$x$$"):
                kind, out = m.last_resort_formula("\\begin{array}{", b"PNGDATA", lambda png, hint, r=reply: r, Path(d) / "assets")
                self.assertEqual(kind, "picture", reply)
                self.assertRegex(out, r"^!\[\[equation_[0-9a-f]{12}\.png\]\]$")
            name = out[3:-2]
            self.assertEqual((Path(d) / "assets" / name).read_bytes(), b"PNGDATA")

    def test_no_image_means_no_fallback(self):
        self.assertEqual(m.last_resort_formula("x", None, lambda *a: "text", Path("/nonexistent")), ("none", None))

    def test_model_error_falls_through_to_picture(self):
        def boom(png, hint):
            raise urllib.error.URLError("down")
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(m.last_resort_formula("x", b"p", boom, Path(d))[0], "picture")

    def test_picture_swaps_cleanly_out_of_its_dollar_wrapper(self):
        out = m.apply_swaps("a\n\n$$PDFMDFORMULA0X$$\n\nb", {"PDFMDFORMULA0X": "![[equation_abc.png]]"})
        self.assertEqual(out, "a\n\n![[equation_abc.png]]\n\nb")


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


class SplitMainSmokeTests(unittest.TestCase):
    def test_split_run_writes_a_folder_and_no_single_note(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d) / "My Book.pdf"
            pdf.write_bytes(b"%PDF-1.4 fake")

            def fake_convert(conv, pdf_, rng, assets, pool, *rest):
                assets.mkdir(parents=True, exist_ok=True)
                (assets / "image_000000_abc.png").write_bytes(b"png")
                return BOOK.split("---\n", 1)[1], m.new_stats()

            with mock.patch.object(m, "make_converter", lambda *a, **k: object()), \
                 mock.patch.object(m, "convert_slice", fake_convert), \
                 mock.patch.object(m, "page_count", lambda p: 10), \
                 mock.patch.object(m, "CACHE_ROOT", Path(d) / "cache"), \
                 mock.patch.object(sys, "argv", ["pdf_to_md", str(pdf), "--no-llm", "--no-backup", "--split-chapters"]):
                m.main()
            folder = Path(d) / "My Book"
            names = sorted(p.name for p in folder.glob("*.md"))
            self.assertEqual(names, ["00 Front matter.md", "00 Index.md", "01 Intro.md", "02 Models and more.md",
                                     "03 Wrap up.md", "90 Bibliography.md", "91 Index.md"])
            self.assertFalse((Path(d) / "My Book.md").exists())
            self.assertTrue((folder / "assets" / "image_000000_abc.png").exists())
            self.assertIn("[[01 Intro", (folder / "00 Index.md").read_text())
            self.assertIn("chapter: 1", (folder / "01 Intro.md").read_text())

    def test_split_falls_back_to_single_note_without_chapters(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4 fake")
            with mock.patch.object(m, "make_converter", lambda *a, **k: object()), \
                 mock.patch.object(m, "convert_slice", lambda *a, **k: ("# Title\n\ntext\n", m.new_stats())), \
                 mock.patch.object(m, "page_count", lambda p: 3), \
                 mock.patch.object(m, "CACHE_ROOT", Path(d) / "cache"), \
                 mock.patch.object(sys, "argv", ["pdf_to_md", str(pdf), "--no-llm", "--no-backup", "--split-chapters"]):
                m.main()
            self.assertTrue((Path(d) / "paper.md").exists())


class StrictRetryTests(unittest.TestCase):
    def test_which_rejections_are_retried(self):
        for why in ("dropped words (recall 0.50)", "invented words (precision 0.40)", "invalid LaTeX (unclosed {)",
                    "length changed too much", "display math", "odd number of $", "no answer", "empty"):
            self.assertTrue(m.should_retry(why), why)
        self.assertFalse(m.should_retry("no math produced"))   # a plain paragraph is a correct outcome

    def test_strict_prompt_demands_exact_words(self):
        for needle in ("EXACTLY", "$", "closing"):
            self.assertIn(needle, m.INLINE_STRICT_SYSTEM)


class BackupHookTests(unittest.TestCase):
    def test_find_vault_root(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d) / "vault"
            (vault / ".obsidian").mkdir(parents=True)
            (vault / "Books" / "sub").mkdir(parents=True)
            self.assertEqual(m.find_vault_root(vault / "Books" / "sub"), vault.resolve())
            self.assertIsNone(m.find_vault_root(Path(d)))

    def test_backup_command_success_and_failure(self):
        ok = f"{sys.executable} -c \"print('{{\\\"archive\\\": \\\"/b/x.zip\\\"}}')\""
        m.run_backup(ok)                                    # prints the archive; must not raise
        with self.assertRaises(SystemExit):
            m.run_backup(f"{sys.executable} -c \"import sys; sys.exit(3)\"")

    def test_backup_runs_only_inside_a_vault_and_before_writing(self):
        from unittest import mock
        for in_vault, expect in ((True, 1), (False, 0)):
            with tempfile.TemporaryDirectory() as d:
                root = Path(d) / "vault"
                (root / ".obsidian").mkdir(parents=True) if in_vault else root.mkdir()
                pdf = root / "a.pdf"
                pdf.write_bytes(b"%PDF-1.4 fake")
                calls = []

                def fake_backup(cmd):
                    calls.append((cmd, list(root.glob("*.md"))))

                with mock.patch.object(m, "make_converter", lambda *a, **k: object()), \
                     mock.patch.object(m, "convert_slice", lambda *a, **k: ("text\n", m.new_stats())), \
                     mock.patch.object(m, "page_count", lambda p: 1), \
                     mock.patch.object(m, "run_backup", fake_backup), \
                     mock.patch.object(m, "CACHE_ROOT", Path(d) / "cache"), \
                     mock.patch.object(sys, "argv", ["pdf_to_md", str(pdf), "--no-llm", "--backup-command", "BACKUP"]):
                    m.main()
                self.assertEqual(len(calls), expect)
                if expect:
                    self.assertEqual(calls[0], ("BACKUP", []))      # backup ran before the note existed
                self.assertTrue((root / "a.md").exists())

    def test_no_backup_flag_wins(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".obsidian").mkdir()
            pdf = Path(d) / "a.pdf"
            pdf.write_bytes(b"%PDF-1.4 fake")
            with mock.patch.object(m, "make_converter", lambda *a, **k: object()), \
                 mock.patch.object(m, "convert_slice", lambda *a, **k: ("text\n", m.new_stats())), \
                 mock.patch.object(m, "page_count", lambda p: 1), \
                 mock.patch.object(m, "run_backup", lambda c: self.fail("backup must not run")), \
                 mock.patch.object(m, "CACHE_ROOT", Path(d) / "cache"), \
                 mock.patch.object(sys, "argv", ["pdf_to_md", str(pdf), "--no-llm", "--no-backup", "--backup-command", "X"]):
                m.main()


if __name__ == "__main__":
    unittest.main()
