"""Equation numbers merged by paddlex (`\\tag*{(1.2)}`) are normalised and survive the maths pipeline (offline)."""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pdf2md_local import guards, notes  # noqa: E402
from pdf2md_local.latex import normalize_math, normalize_tags, simplify_light_math, validate_document  # noqa: E402

F = r"E = \frac{1}{2} \sum_{n=1}^{N} \{ y(x_n, w) - t_n \}^2"


def block(tag):
    return f"Before.\n\n$${F} {tag}$$\n\nAfter.\n"


class Tags(unittest.TestCase):
    def test_normalisation(self):
        for raw, want in [(r"\tag*{(1.2)}", r"\tag{1.2}"), (r"\tag*{1.2}", r"\tag{1.2}"), (r"\tag*{ ( A.4 ) }", r"\tag{A.4}"),
                          (r"\tag{(3)}", r"\tag{3}"), (r"\tag*{(1 . 2)}", r"\tag{1.2}"), (r"\tag*{(2.1a)}", r"\tag{2.1a}"),
                          (r"\tag*{(\text{1.2})}", r"\tag{1.2}")]:
            out = normalize_math(block(raw))
            self.assertIn(f"{F.replace('y(x_n, w)', 'y(x_n, w)')} {want}\n$$".replace("{ y", "{y"), out.replace("\\{ y", "\\{y"), raw)

    def test_garbage_dropped(self):
        for raw in (r"\tag*{(x y z)}", r"\tag*{()}", r"\tag*{(see text)}"):
            self.assertNotIn(r"\tag", normalize_math(block(raw)), raw)

    def test_idempotent_and_validates(self):
        for raw in (r"\tag*{(1.2)}", r"\tag{A.4}"):
            once = normalize_math(block(raw))
            self.assertEqual(normalize_math(once), once)
            self.assertEqual(validate_document(once), [])
            self.assertEqual(simplify_light_math(once), once)
            self.assertEqual(notes.clean_paddle_markdown(block(raw), 1, None, None).count(r"\tag"), 1)

    def test_tag_with_aligned_environment(self):
        out = normalize_math("$$a &= b \\\\ c &= d \\tag*{(2.1)}$$")
        self.assertIn("\\begin{aligned}\na &= b \\\\ c &= d\n\\end{aligned} \\tag{2.1}", out)
        self.assertEqual(validate_document(out), [])

    def test_last_tag_wins_and_text_untouched(self):
        out = normalize_math("$$x \\tag*{(1.1)} y \\tag*{(1.2)}$$")
        self.assertEqual(len(re.findall(r"\\tag", out)), 1)
        self.assertIn(r"\tag{1.2}", out)

    def test_normalize_tags_direct(self):
        self.assertEqual(normalize_tags(r"x \tag*{(1.2)}"), r"x \tag{1.2}")
        self.assertEqual(normalize_tags("x"), "x")

    def test_guards_ignore_tags(self):
        tex = F + r" \tag{1.2}"
        self.assertEqual(guards.native_variable_problems(tex, "E = 1/2 sum y(xn,w) - tn 2"),
                         guards.native_variable_problems(F, "E = 1/2 sum y(xn,w) - tn 2"))
        self.assertEqual(guards.formula_retry_problems(F + r" \tag*{(1.2)}"), guards.formula_retry_problems(F))
        self.assertEqual(guards.formula_body(f"$${F} \\tag*{{(1.2)}}$$").count("tag"), 1)

    def test_worker_requests_formula_numbers(self):
        src = (Path(__file__).resolve().parents[2] / "pdf2md_local" / "paddle_worker.py").read_text()
        self.assertIn("show_formula_number=True", src)


if __name__ == "__main__":
    unittest.main()
