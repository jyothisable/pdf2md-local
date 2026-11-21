"""Anchors and cross-reference links (offline)."""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pdf2md_local import notes as notes_mod  # noqa: E402
from pdf2md_local.structure import apply_callouts  # noqa: E402
from pdf2md_local.xref import add_cross_links  # noqa: E402


def run(bodies, plain=False, callouts=False):
    ns = [{"file": f, "body": apply_callouts(b, plain)[0] if callouts else b} for f, b in bodies]
    n = add_cross_links(ns, plain)
    return [x["body"] for x in ns], n


class Anchors(unittest.TestCase):
    def test_tag_equation_obsidian(self):
        (b,), n = run([("A", "Intro.\n\n$$\nx=1 \\tag{3.2}\n$$\n\nNext.\n")])
        self.assertEqual(b, "Intro.\n\n$$\nx=1 \\tag{3.2}\n$$\n\n^eq-3-2\n\nNext.\n")
        self.assertEqual(n, 0)

    def test_trailing_number_forms(self):
        for tail in ["\\quad (3.2)", "\\qquad(3.2)", "\\qquad \\text{(3.2)}"]:
            (b,), _ = run([("A", f"$$\nx=1 {tail}\n$$\n")])
            self.assertIn("^eq-3-2", b, tail)
        (b,), _ = run([("A", "$$\ny = f(2)\n$$\n")])  # not a number tag
        self.assertNotIn("^eq", b)

    def test_equation_anchor_untouched_math(self):
        src = "$$\nx=1 \\quad (3.2)\n$$\n"
        (b,), _ = run([("A", src)])
        self.assertTrue(b.startswith(src))

    def test_plain_anchor(self):
        (b,), _ = run([("A", "Intro.\n$$\nx \\tag{1}\n$$\n")], plain=True)
        self.assertEqual(b, 'Intro.\n\n<a id="eq-1"></a>\n\n$$\nx \\tag{1}\n$$\n')

    def test_caption_paragraph_ids(self):
        (b,), _ = run([("A", "Figure 4.1. A cat.\n\n**Table 2.3:** Data.\n\nFig. 5 shows more.\n\nFig. 6. Dog\n")])
        self.assertIn("Figure 4.1. A cat. ^fig-4-1", b)
        self.assertIn("**Table 2.3:** Data. ^tab-2-3", b)
        self.assertNotIn("fig-5", b)
        self.assertIn("Fig. 6. Dog ^fig-6", b)

    def test_label_targets_with_and_without_callouts(self):
        src = "**Theorem 2.1** (N). Text.\n\nLemma 2.2. Other.\n\nProof. P ∎\n"
        (b,), _ = run([("A", src)], callouts=False)
        self.assertIn("Text. ^thm-2-1", b)
        self.assertIn("Other. ^lem-2-2", b)
        self.assertNotIn("proof", b.lower().replace("proof.", ""))
        (b,), _ = run([("A", src)], callouts=True)
        self.assertIn("> [!theorem] Theorem 2.1 (N)\n> Text.\n\n^thm-2-1\n", b)
        self.assertIn("> [!lemma] Lemma 2.2\n> Other.\n\n^lem-2-2\n", b)
        self.assertEqual(b.count("^"), 2)

    def test_callout_plain_quote_anchor(self):
        (b,), _ = run([("A", "Definition 1.2. D.\n")], plain=True, callouts=True)
        self.assertEqual(b, '<a id="def-1-2"></a>\n\n> **Definition 1.2.** D.\n')

    def test_equation_in_plain_quote(self):
        (b,), _ = run([("A", "Theorem 1. T\n\n$$\nx \\tag{1.1}\n$$\n")], plain=True, callouts=True)
        self.assertIn("> <a id=\"eq-1-1\"></a>\n>\n> $$", b)

    def test_equation_in_obsidian_callout_anchors_callout(self):
        (b,), _ = run([("A", "Proof. P\n\n$$\nx \\tag{1.1}\n$$\n\n## H\n\nSee Eq. (1.1).\n")], callouts=True)
        self.assertIn("> $$\n\n^eq-1-1\n\n## H", b)
        self.assertIn("[[#^eq-1-1|Eq. (1.1)]]", b)

    def test_duplicate_ids_first_wins(self):
        (a, c), _ = run([("A", "Figure 1.1. one\n\nFigure 1.1. two\n"), ("B", "Figure 1.1. three\n\nsee Figure 1.1\n")])
        self.assertEqual(len(re.findall(r"(?m)(?:^| )\^fig-1-1$", a + c)), 1)
        self.assertIn("[[A#^fig-1-1|Figure 1.1]]", c)


class Links(unittest.TestCase):
    BASE = "Figure 4.1. A cat.\n\n$$\nx \\tag{3.2}\n$$\n\nTheorem 2.1. T.\n"

    def test_same_note_keeps_text(self):
        (b,), n = run([("A", self.BASE + "\nBy Eq. (3.2) and Fig. 4.1, plus Equation (3.2).\n")])
        self.assertEqual(n, 3)
        self.assertIn("By [[#^eq-3-2|Eq. (3.2)]] and [[#^fig-4-1|Fig. 4.1]], plus [[#^eq-3-2|Equation (3.2)]].", b)

    def test_cross_note(self):
        (a, c), n = run([("01 Chap one", self.BASE), ("02 Two", "As in Eq. (3.2), Table 9, Theorem 2.1 holds.\n")])
        self.assertEqual(n, 2)
        self.assertIn("As in [[01 Chap one#^eq-3-2|Eq. (3.2)]], Table 9, [[01 Chap one#^thm-2-1|Theorem 2.1]] holds.", c)

    def test_cross_note_plain_url(self):
        (a, c), _ = run([("01 Chap one", self.BASE), ("02 Two", "As in Eq. (3.2) and Fig. 4.1.\n")], plain=True)
        self.assertIn("[Eq. (3.2)](01%20Chap%20one.md#eq-3-2)", c)
        self.assertIn("[Fig. 4.1](01%20Chap%20one.md#fig-4-1)", c)
        (b,), _ = run([("A", self.BASE + "\nSee Eq. (3.2).\n")], plain=True)
        self.assertIn("[Eq. (3.2)](#eq-3-2)", b)

    def test_bare_number_chain(self):
        (b,), _ = run([("A", "$$\na \\tag{1.1}\n$$\n\n$$\nb \\tag{1.2}\n$$\n\nSee Eqs. (1.1) and (1.2); also (1.1) alone, and (1.2).\n")])
        self.assertIn("[[#^eq-1-1|Eqs. (1.1)]] and [[#^eq-1-2|(1.2)]]; also [[#^eq-1-1|(1.1)]] alone, and [[#^eq-1-2|(1.2)]].", b)

    def test_plural_figure_chain(self):
        (b,), _ = run([("A", "Figure 1.1. a\n\nFigure 1.2. b\n\nSee Figs. 1.1 and 1.2.\n")])
        self.assertIn("[[#^fig-1-1|Figs. 1.1]] and [[#^fig-1-2|1.2]]", b)

    def test_midparagraph_label_reference_in_callout_world(self):
        src = "Theorem 2.1. T.\n\n## H\n\nSo Theorem 2.1 shows that x.\n"
        (b,), n = run([("A", src)], callouts=True)
        self.assertIn("So [[#^thm-2-1|Theorem 2.1]] shows that x.", b)
        self.assertEqual(n, 1)

    def test_no_self_link_in_target(self):
        (b,), n = run([("A", "Theorem 2.1. As Theorem 2.1 says.\n\nFigure 1.1. In Figure 1.1 we see.\n")])
        self.assertEqual(n, 0)
        self.assertNotIn("[[", b)

    def test_unknown_target_untouched(self):
        src = "See Eq. (9.9) and Figure 8.8 and Lemma 3.\n"
        self.assertEqual(run([("A", src)])[0][0], src)

    def test_protected_regions(self):
        head = "$$\nx \\tag{3.2}\n$$\n\n"
        prot = ("Inline $see Eq. (3.2)$ and `Eq. (3.2)` and [Eq. (3.2)](http://x) and [[Eq. (3.2)]] and "
                "<!-- Eq. (3.2) -->.\n\n$$\ny \\text{Eq. (3.2)}\n$$\n\n## About Eq. (3.2)\n\n| Eq. (3.2) |\n|---|\n\n"
                "```\nEq. (3.2)\n```\n\n![[Eq. (3.2).png]]\n")
        for plain in (False, True):
            (b,), n = run([("A", head + prot)], plain=plain)
            self.assertEqual(n, 0)
            self.assertTrue(b.endswith(prot))

    def test_idempotent(self):
        for plain in (False, True):
            for co in (False, True):
                bodies = [("01 A", self.BASE + "\nSee Eq. (3.2).\n"), ("02 B", "Fig. 4.1 and Theorem 2.1.\n")]
                ns = [{"file": f, "body": apply_callouts(b, plain)[0] if co else b} for f, b in bodies]
                add_cross_links(ns, plain)
                snap = [x["body"] for x in ns]
                self.assertEqual(add_cross_links(ns, plain), 0)
                self.assertEqual([x["body"] for x in ns], snap)

    def test_to_plain_interaction(self):
        (a, c), _ = run([("01 Chap one", self.BASE + "\nsee Eq. (3.2)\n"), ("02 Two", "As in Eq. (3.2).\n")], plain=True)
        # plain output survives to_plain unchanged
        for body in (a, c):
            self.assertEqual(notes_mod.to_plain(body, "assets", "x.pdf"), body)
        # obsidian block links are left alone by to_plain (never half-converted)
        (a, c), _ = run([("01 Chap one", self.BASE), ("02 Two", "As in Eq. (3.2).\n")])
        conv = notes_mod.to_plain(c, "assets", "x.pdf")
        self.assertEqual(conv, c)
        self.assertEqual(notes_mod.to_plain("[[#^eq-1|x]]", "assets", "x.pdf"), "[[#^eq-1|x]]")

    def test_page_markers_and_failed_markers(self):
        src = ("<!-- pdf2md:page=3 -->\n\nFigure 1.1. c\n\n<!-- pdf2md:failed page=4 -->\n\nsee Fig. 1.1\n")
        (b,), n = run([("A", src)])
        self.assertEqual(n, 1)
        self.assertIn("<!-- pdf2md:page=3 -->", b)
        self.assertIn("<!-- pdf2md:failed page=4 -->", b)

    def test_letter_suffix_falls_back(self):
        (b,), _ = run([("A", "Figure 2.1. c\n\nsee Figure 2.1a\n")])
        self.assertIn("[[#^fig-2-1|Figure 2.1a]]", b)


class RealBookPatterns(unittest.TestCase):
    EQS = "$$\na \\tag{1.2}\n$$\n\n$$\nb \\tag{1.4}\n$$\n\n$$\nc \\tag{A.4}\n$$\n\n"

    def test_dotted_caption_without_delimiter(self):
        (b,), _ = run([("A", "Figure 1.2 Plot of a training set of points.\n\nTable 3.1 Summary of data.\n\n"
                             "Figure 1.7 shows the results.\n\nFig. 2.4: Colon form.\n")])
        self.assertIn("points. ^fig-1-2", b)
        self.assertIn("data. ^tab-3-1", b)
        self.assertIn("form. ^fig-2-4", b)
        self.assertNotIn("fig-1-7", b)
        self.assertNotIn("^fig-1\n", b + "\n")
        self.assertNotRegex(b, r"\^fig-1(?![-\w])")

    def test_dotted_caption_with_period_and_bold(self):
        (b,), _ = run([("A", "**Figure 1.2.** Plot.\n\n**Table 2.3** Data\n")])
        self.assertIn("^fig-1-2", b)
        self.assertIn("^tab-2-3", b)

    def test_tag_star_with_parens_gives_id(self):
        (b,), _ = run([("A", "$$\nx \\tag*{(1.2)}\n$$\n")])
        self.assertIn("^eq-1-2", b)

    def test_bare_dotted_equation_references(self):
        (b,), n = run([("A", self.EQS + "The error function (1.2) is given by (1.4), see also (A.4) and (3) and (1.9).\n")])
        self.assertIn("function [[#^eq-1-2|(1.2)]] is given by [[#^eq-1-4|(1.4)]], see also [[#^eq-A-4|(A.4)]] and (3) and (1.9).", b)
        self.assertEqual(n, 3)

    def test_bare_range_and_plain(self):
        (b,), _ = run([("A", self.EQS + "Using (1.2)–(1.4) here.\n")])
        self.assertIn("[[#^eq-1-2|(1.2)]]–[[#^eq-1-4|(1.4)]]", b)
        (b,), _ = run([("A", self.EQS + "Using (1.2)–(1.4) here.\n")], plain=True)
        self.assertIn("[(1.2)](#eq-1-2)–[(1.4)](#eq-1-4)", b)

    def test_bare_not_inside_math_code_links_or_headings(self):
        tail = "$x(1.2)$ and `(1.2)` and [t (1.2)](u) and [[n|(1.2)]]\n\n## Heading (1.2)\n\n| (1.2) |\n|-|\n"
        (b,), n = run([("A", self.EQS + tail)])
        self.assertTrue(b.endswith(tail))
        self.assertEqual(n, 0)

    def test_eq_prefixed_and_bare_do_not_overlap(self):
        (b,), n = run([("A", self.EQS + "By Eq. (1.2) and (1.4).\n")])
        self.assertIn("By [[#^eq-1-2|Eq. (1.2)]] and [[#^eq-1-4|(1.4)]].", b)
        self.assertEqual(n, 2)

    HEADS = ("# 1. INTRODUCTION\n\nintro\n\n## 1.2 Probability Theory\n\ntext\n\n### 1.2.4 The Gaussian distribution\n\nx\n\n"
             "## 1.3 Model: Selection\n\n")

    def test_section_links_obsidian(self):
        (b,), n = run([("A", self.HEADS + "See Section 1.2.4 and Sections 1.2 and 1.3, Chapter 1.\n")])
        self.assertIn("See [[#1.2.4 The Gaussian distribution|Section 1.2.4]] and [[#1.2 Probability Theory|Sections 1.2]] and "
                      "[[#1.3 Model: Selection|1.3]], [[#1. INTRODUCTION|Chapter 1]].", b)
        self.assertEqual(n, 4)

    def test_section_links_not_in_plain(self):
        src = self.HEADS + "See Section 1.2.4.\n"
        (b,), n = run([("A", src)], plain=True)
        self.assertEqual((b, n), (src, 0))

    def test_section_ambiguous_or_unsafe_skipped(self):
        src = ("# 1. INTRODUCTION\n\n# 1. INTRODUCTION\n\n## 2.1 One\n\n## 2.1 Two\n\n## 3.1 A|B\n\n## 4.1 C #x\n\n"
               "Chapter 1, Section 2.1, Section 3.1, Section 4.1, Section 9.9.\n")
        (b,), n = run([("A", src)])
        self.assertEqual((b, n), (src, 0))

    def test_section_across_notes_and_split_chapters(self):
        notes = [{"file": "01 Intro", "title": "Intro", "chapter": 1, "body": "## 1.1 Start\n\ntext\n"},
                 {"file": "02 Next", "title": "Next", "chapter": 2, "body": "Back to Section 1.1 and Chapter 1; Chapter 2.\n"}]
        add_cross_links(notes, False)
        self.assertEqual(notes[1]["body"], "Back to [[01 Intro#1.1 Start|Section 1.1]] and [[01 Intro#1 Intro|Chapter 1]]; "
                                           "[[#2 Next|Chapter 2]].\n")

    def test_section_idempotent(self):
        ns = [{"file": "A", "body": self.HEADS + "See Section 1.2.4.\n"}]
        add_cross_links(ns, False)
        once = ns[0]["body"]
        self.assertEqual(add_cross_links(ns, False), 0)
        self.assertEqual(ns[0]["body"], once)


if __name__ == "__main__":
    unittest.main()
