"""Callouts and cross-reference links wired into the CLI (offline, fake worker)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pdf2md_local import convert as m  # noqa: E402
from pdf2md_local.latex import validate_document  # noqa: E402
from test_cli import Env, make_pdf  # noqa: E402
from test_pdf_to_md import BOOK  # noqa: E402

TWO = """# 1 Intro

Intro text.

**Theorem 1.1** (Euler). Let $x$ be real.

$$
e^{i\\pi}+1=0 \\tag{1.1}
$$

*Proof.* Trivial. $\\square$

Figure 1.1. A plot.

# 2 Models

We use Eq. (1.1) and Theorem 1.1 together with Fig. 1.1.
"""
DOC = "<!-- pdf2md:page=1 -->\n\n" + TWO.split("# 2 Models")[0] + "<!-- pdf2md:page=2 -->\n\n# 2 Models" + TWO.split("# 2 Models")[1]


def result(env):
    line = [l for l in env.out.getvalue().splitlines() if l.startswith("RESULT ")]
    return json.loads(line[0][7:])


def convert(d, extra, md=DOC, name="Book.pdf"):
    env = Env(d, pages=10, md=md).run([make_pdf(d, name), "--split-chapters", "--no-backup", *extra])
    return env, Path(d) / "Book"


class CliStructure(unittest.TestCase):
    def test_obsidian_gets_callouts_links_and_block_ids(self):
        with tempfile.TemporaryDirectory() as d:
            env, folder = convert(d, ["--flavor", "obsidian"])
            one = (folder / "01 Intro.md").read_text()
            two = (folder / "02 Models.md").read_text()
            self.assertIn("> [!theorem] Theorem 1.1 (Euler)", one)
            self.assertIn("> [!proof] Proof", one)
            self.assertIn("^thm-1-1", one)
            self.assertIn("^fig-1-1", one)
            self.assertIn("[[01 Intro#^thm-1-1|Theorem 1.1]]", two)
            self.assertIn("[[01 Intro#^fig-1-1|Fig. 1.1]]", two)
            r = result(env)
            self.assertEqual(r["callouts"], 2)
            self.assertGreaterEqual(r["cross_links"], 2)
            self.assertIn(f"structure: 2 callout(s), {r['cross_links']} cross-reference link(s)", env.out.getvalue())

    def test_plain_auto_has_neither_and_matches_off(self):
        outs = []
        for extra in (["--flavor", "plain"], ["--flavor", "plain", "--callouts", "off", "--cross-links", "off"]):
            with tempfile.TemporaryDirectory() as d:
                env, folder = convert(d, extra)
                outs.append({p.name: p.read_text().replace(d, "") for p in sorted(folder.glob("*.md"))})
                r = result(env)
                self.assertEqual((r["callouts"], r["cross_links"]), (0, 0))
                self.assertNotIn("structure:", env.out.getvalue())
                self.assertNotIn("[!theorem]", outs[-1]["01 Intro.md"])
                self.assertNotIn("<a id", outs[-1]["01 Intro.md"])
        self.assertEqual(outs[0], outs[1])

    def test_plain_on_gives_blockquote_and_html_anchors(self):
        with tempfile.TemporaryDirectory() as d:
            env, folder = convert(d, ["--flavor", "plain", "--callouts", "on", "--cross-links", "on"])
            one = (folder / "01 Intro.md").read_text()
            two = (folder / "02 Models.md").read_text()
            self.assertIn("> **Theorem 1.1 (Euler).** Let $x$ be real.", one)
            self.assertIn('<a id="thm-1-1"></a>', one)
            self.assertIn('<a id="eq-1-1"></a>', one)
            self.assertIn("[Theorem 1.1](01%20Intro.md#thm-1-1)", two)
            self.assertIn("[Eq. (1.1)](01%20Intro.md#eq-1-1)", two)
            self.assertNotIn("[[", one + two)

    def test_obsidian_off_flags(self):
        with tempfile.TemporaryDirectory() as d:
            env, folder = convert(d, ["--flavor", "obsidian", "--callouts", "off", "--cross-links", "off"])
            self.assertNotIn("[!theorem]", (folder / "01 Intro.md").read_text())
            self.assertNotIn("#^", (folder / "02 Models.md").read_text())
            self.assertEqual((result(env)["callouts"], result(env)["cross_links"]), (0, 0))

    def test_single_note(self):
        with tempfile.TemporaryDirectory() as d:
            env = Env(d, pages=10, md=DOC).run([make_pdf(d, "Paper.pdf"), "--no-backup", "--flavor", "obsidian"])
            text = (Path(d) / "Paper.md").read_text()
            self.assertIn("> [!theorem] Theorem 1.1 (Euler)", text)
            self.assertIn("[[#^thm-1-1|Theorem 1.1]]", text)
            self.assertNotIn("pdf2md:page", text)
            self.assertGreater(result(env)["cross_links"], 0)

    def test_page_markers_kept(self):
        with tempfile.TemporaryDirectory() as d:
            env, folder = convert(d, ["--flavor", "obsidian", "--page-markers"])
            one = (folder / "01 Intro.md").read_text()
            self.assertIn("<!-- pdf2md:page=2 -->", one)
            self.assertIn("> [!theorem]", one)
            self.assertEqual(result(env)["callouts"], 2)

    def test_math_warnings_unchanged_by_enrichment(self):
        bad = DOC.replace("Trivial.", "Trivial $\\frac{1$.")
        counts = []
        for extra in (["--callouts", "off", "--cross-links", "off"], []):
            with tempfile.TemporaryDirectory() as d:
                env, folder = convert(d, ["--flavor", "obsidian", *extra], md=bad)
                r = result(env)
                counts.append(sorted((Path(w["note"]).name, w["count"]) for w in r["math_warnings"]))
                one = (folder / "01 Intro.md").read_text()
                self.assertIn(f"math_warnings: {counts[-1][0][1] if counts[-1] else 0}", one)
                if not extra:  # final body re-validates to the same count
                    body = one.split("# 1 Intro", 1)[1]
                    self.assertEqual(len(validate_document(body)), counts[-1][0][1] if counts[-1] else 0)
        self.assertEqual(counts[0], counts[1])
        self.assertTrue(counts[0])

    def test_flags_not_in_cache_key(self):
        keys = []
        for extra in ([], ["--callouts", "on", "--cross-links", "on"], ["--callouts", "off", "--cross-links", "off"]):
            with tempfile.TemporaryDirectory() as d:
                seen = []
                orig = m.run_key
                m.run_key = lambda pdf, *parts, _o=orig, _s=seen: (_s.append(parts), _o(pdf, *parts))[1]
                try:
                    Env(d, pages=10, md=DOC).run([make_pdf(d, "Book.pdf"), "--no-backup", "--flavor", "obsidian", *extra])
                finally:
                    m.run_key = orig
                keys.append(seen[0])
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(keys[0], keys[2])

    def test_example_style_book_plain_unchanged(self):
        outs = []
        for extra in ([], ["--callouts", "off", "--cross-links", "off"]):
            with tempfile.TemporaryDirectory() as d:
                Env(d, pages=10, md=BOOK.split("---\n", 1)[1]).run(
                    [make_pdf(d, "My Book.pdf"), "--split-chapters", "--flavor", "plain", "--no-backup", *extra])
                folder = Path(d) / "My Book"
                outs.append({p.name: p.read_text().replace(d, "") for p in sorted(folder.glob("*.md"))})
        self.assertEqual(outs[0], outs[1])


if __name__ == "__main__":
    unittest.main()
