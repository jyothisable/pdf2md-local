"""Theorem-like callouts (offline)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pdf2md_local.structure import apply_callouts  # noqa: E402


def ob(md):
    return apply_callouts(md, False)


def pl(md):
    return apply_callouts(md, True)


class Callouts(unittest.TestCase):
    def test_theorem_with_display_math_and_proof(self):
        md = ("Intro.\n\n**Theorem 2.1** (Pythagoras). For a right triangle,\n\n$$\na^2+b^2=c^2\n$$\n\n"
              "where c is long.\n\n*Proof.* Draw.\n\nMore. ∎\n\nAfter the proof.\n")
        out, n = ob(md)
        self.assertEqual(n, 2)
        self.assertIn("> [!theorem] Theorem 2.1 (Pythagoras)\n> For a right triangle,\n>\n> $$\n> a^2+b^2=c^2\n> $$\n>\n> where c is long.\n", out)
        self.assertIn("> [!proof] Proof\n> Draw.\n>\n> More. ∎\n\nAfter the proof.", out)
        self.assertTrue(out.startswith("Intro.\n\n"))

    def test_definition_with_name_and_colon(self):
        out, n = ob("**Definition 3.4 (Group):** A set with an operation.\n")
        self.assertEqual(n, 1)
        self.assertEqual(out, "> [!definition] Definition 3.4 (Group)\n> A set with an operation.\n")

    def test_label_forms(self):
        for src, head in [("**Lemma 1.** x", "> [!lemma] Lemma 1"), ("*Remark.* x", "> [!note] Remark"),
                          ("Exercise 2.3. x", "> [!question] Exercise 2.3"), ("Corollary A.4: x", "> [!corollary] Corollary A.4"),
                          ("**Proposition 5**. x", "> [!proposition] Proposition 5"), ("Example. x", "> [!example] Example"),
                          ("**Theorem 2.1.** x", "> [!theorem] Theorem 2.1")]:
            out, n = ob(src)
            self.assertEqual(n, 1, src)
            self.assertTrue(out.startswith(head + "\n> x"), (src, out))

    def test_non_labels(self):
        for src in ["Theorem 2.1 shows that x.", "We use Theorem 2.1. It helps.", "theorem 2.1. x", "Proofs. x",
                    "Prooftext: x", "**Theorem 2.1 x", "Proof of Theorem 2. x", "Theorems: x"]:
            self.assertEqual(ob(src), (src, 0), src)

    def test_label_only_at_paragraph_start(self):
        md = "Some text\nTheorem 2.1. continues the paragraph\n"
        self.assertEqual(ob(md), (md, 0))

    def test_stops(self):
        md = "Definition 1. A.\n\nB para.\n\nLemma 2. C.\n\nD.\n\n## Heading\n\nE\n"
        out, n = ob(md)
        self.assertEqual(n, 2)
        self.assertIn("> [!definition] Definition 1\n> A.\n>\n> B para.\n\n> [!lemma] Lemma 2\n> C.\n>\n> D.\n\n## Heading\n\nE", out)

    def test_table_and_image_stop(self):
        out, n = ob("Example 1. See\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nAfter.\n")
        self.assertEqual(out, "> [!example] Example 1\n> See\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nAfter.\n")
        out, _ = ob("Remark. x\n\n![[a.jpg]]\n\ny\n")
        self.assertEqual(out, "> [!note] Remark\n> x\n\n![[a.jpg]]\n\ny\n")

    def test_page_marker_is_not_a_stop_and_is_strippable(self):
        from pdf2md_local.notes import strip_page_markers
        out, _ = ob("Theorem 1. a\n\n<!-- pdf2md:page=4 -->\n\nb\n")
        self.assertEqual(out, "> [!theorem] Theorem 1\n> a\n>\n> <!-- pdf2md:page=4 -->\n>\n> b\n")
        self.assertEqual(strip_page_markers(out), "> [!theorem] Theorem 1\n> a\n>\n> b\n")

    def test_trailing_page_marker_stays_outside(self):
        out, _ = ob("Theorem 1. a\n\n<!-- pdf2md:page=4 -->\n\n## H\n")
        self.assertEqual(out, "> [!theorem] Theorem 1\n> a\n\n<!-- pdf2md:page=4 -->\n\n## H\n")

    def test_failed_marker_still_stops(self):
        out, _ = ob("Theorem 1. a\n\n<!-- pdf2md:failed page=4 -->\n\nb\n")
        self.assertEqual(out, "> [!theorem] Theorem 1\n> a\n\n<!-- pdf2md:failed page=4 -->\n\nb\n")

    def test_caps(self):
        paras = "\n\n".join(f"p{i}" for i in range(1, 9))
        out, _ = ob("Theorem 1. first\n\n$$\nx\n$$\n\n" + paras + "\n")
        # cap hit: label paragraph and directly following display math only
        self.assertIn("> [!theorem] Theorem 1\n> first\n>\n> $$\n> x\n> $$\n\np1\n", out)
        proof = "\n\n".join(f"q{i}" for i in range(1, 15))
        out, _ = ob("Proof. start\n\n" + proof + "\n")
        self.assertIn("> [!proof] Proof\n> start\n\nq1\n", out)
        out, _ = ob("Proof. start\n\nq1\n\nq2 □\n\nq3")
        self.assertIn("> q2 □\n\nq3", out)

    def test_square_end_marks(self):
        out, _ = ob("Proof. x $\\blacksquare$\n\nnext\n")
        self.assertEqual(out, "> [!proof] Proof\n> x $\\blacksquare$\n\nnext\n")

    def test_plain_blockquote(self):
        out, n = pl("**Theorem 2.1** (Name). Body.\n\n$$\nx\n$$\n\nnext\n")
        self.assertEqual(n, 1)
        self.assertEqual(out, "> **Theorem 2.1 (Name).** Body.\n>\n> $$\n> x\n> $$\n>\n> next\n")

    def test_math_code_and_comments_untouched(self):
        md = "```\nTheorem 1. code\n```\n\n$$\nTheorem 1. m\n$$\n\n<!-- Theorem 1. -->\n\n> Theorem 1. quoted\n"
        self.assertEqual(ob(md), (md, 0))

    def test_idempotent(self):
        md = "Theorem 1. a\n\n$$\nx\n$$\n\nProof. b ∎\n\nDefinition 2. c\n"
        for fn in (ob, pl):
            once, _ = fn(md)
            self.assertEqual(fn(once), (once, 0))

    def test_failed_marker_and_blank_whitespace_lines(self):
        md = "Lemma 3. a\n \n$$\nx\n$$\n \n<!-- pdf2md:failed page=2 -->\n"
        out, n = ob(md)
        self.assertEqual(n, 1)
        self.assertIn("<!-- pdf2md:failed page=2 -->", out)
        self.assertIn("> $$", out)


if __name__ == "__main__":
    unittest.main()
