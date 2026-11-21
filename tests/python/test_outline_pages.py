import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pdf2md_local import backend, convert as m  # noqa: E402
from pdf2md_local.runner import parse_page_spec, parse_pages  # noqa: E402
from test_cli import Env, make_pdf  # noqa: E402

HAS_PYPDF = importlib.util.find_spec("pypdf") is not None


def outline_json(pdf):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        backend.main(["outline", str(pdf), "--json"])
    return json.loads(buf.getvalue())


class SpecTests(unittest.TestCase):
    def test_parse_sort_merge_clamp(self):
        self.assertEqual(parse_page_spec("60-80,10-40,95", 200), ([(10, 40), (60, 80), (95, 95)], "10-40,60-80,95"))
        self.assertEqual(parse_page_spec("1-5, 6-9,8-12", 100), ([(1, 12)], "1-12"))
        self.assertEqual(parse_page_spec("5,5,6", 100), ([(5, 6)], "5-6"))
        self.assertEqual(parse_page_spec("90-500,300", 100), ([(90, 100)], "90-100"))
        self.assertEqual(parse_page_spec(None, 50), ([(1, 50)], None))
        self.assertEqual(parse_page_spec("10-20", None), ([(10, 20)], "10-20"))

    def test_old_parse_pages_unchanged(self):
        self.assertEqual(parse_pages("10-40", 100), (10, 40))
        self.assertEqual(parse_pages("7", 100), (7, 7))
        self.assertEqual(parse_pages(None, 100), (1, 100))
        self.assertEqual(parse_pages("90-500", 100), (90, 100))

    def test_malformed_specs_are_errors(self):
        for bad in ("5-3", "abc", "0", "1-", "1,,2", "-4", "1-2-3", "500"):
            with self.assertRaises(SystemExit, msg=bad) as cm:
                parse_page_spec(bad, 100)
            self.assertTrue(str(cm.exception.code).startswith("error:"))


class OutlineTests(unittest.TestCase):
    def test_last_page_rules(self):
        raw = [{"level": 1, "title": "A", "page": 3}, {"level": 2, "title": "A.1", "page": 3},
               {"level": 2, "title": "A.2", "page": 5}, {"level": 2, "title": "A.3", "page": 5},
               {"level": 1, "title": "B", "page": 5},
               {"level": 1, "title": "C", "page": 9}]
        got = [(e["title"], e["first"], e["last"]) for e in backend.outline_entries(raw, 12)]
        self.assertEqual(got, [("A", 3, 4), ("A.1", 3, 4), ("A.2", 5, 5), ("A.3", 5, 5), ("B", 5, 8), ("C", 9, 12)])
        self.assertEqual(backend.outline_entries([], 12), [])

    @unittest.skipUnless(HAS_PYPDF, "pypdf optional")
    def test_json_for_nested_bookmarks_and_none(self):
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as d, mock.patch.object(backend, "rate_file", lambda: Path(d) / "rate.json"):
            w = PdfWriter()
            for _ in range(10):
                w.add_blank_page(width=100, height=100)
            c1 = w.add_outline_item("1 Intro", 1)
            w.add_outline_item("1.1 Scope", 2, parent=c1)
            w.add_outline_item("1.2 Aims", 4, parent=c1)
            w.add_outline_item("2 Methods", 6)
            pdf = Path(d) / "b.pdf"
            w.write(pdf)
            data = outline_json(pdf)
            self.assertEqual(set(data), {"pages", "sec_per_page", "measured", "entries"})
            self.assertEqual((data["pages"], data["measured"], data["sec_per_page"]), (10, False, backend.DEFAULT_SEC_PER_PAGE))
            self.assertEqual(data["entries"], [
                {"title": "1 Intro", "level": 1, "first": 2, "last": 6},
                {"title": "1.1 Scope", "level": 2, "first": 3, "last": 4},
                {"title": "1.2 Aims", "level": 2, "first": 5, "last": 6},
                {"title": "2 Methods", "level": 1, "first": 7, "last": 10}])
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                backend.main(["outline", str(pdf)])
            self.assertIn("  1.1 Scope", buf.getvalue())
            self.assertIn("pp. 7–10", buf.getvalue())
            w2 = PdfWriter()
            for _ in range(3):
                w2.add_blank_page(width=100, height=100)
            plain = Path(d) / "n.pdf"
            w2.write(plain)
            self.assertEqual(outline_json(plain)["entries"], [])
            self.assertEqual(outline_json(plain)["pages"], 3)


class ConvertListTests(unittest.TestCase):
    def test_only_selected_pages_run(self):
        with tempfile.TemporaryDirectory() as d:
            env = Env(d, pages=200, md=lambda r: f"Text {r[0]}-{r[1]}.\n").run(
                [make_pdf(d), "--pages", "60-70,10-20,95", "--pages-per-batch", "6", "--no-backup"])
            ranges = [s[1] for s in env.slices]
            self.assertEqual(ranges, [(10, 15), (16, 20), (60, 65), (66, 70), (95, 95)])
            self.assertEqual(env.progress()[0], "PROGRESS 0 23")
            self.assertEqual(env.progress()[-1], "PROGRESS 23 23")
            note = next(Path(d).glob("paper*.md")).read_text()
            self.assertIn('pages: "10-20,60-70,95"\n', note)
            self.assertNotIn("Text 30", note)

    def test_rate_learning_uses_selected_pages(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(backend, "record_rate") as rec:
            Env(d, pages=300).run([make_pdf(d), "--pages", "1-30,100-129"])
            self.assertEqual(rec.call_args[0][0], 60)

    def test_cache_key_differs_and_failed_retry_resumes(self):
        with tempfile.TemporaryDirectory() as d:
            pdf = make_pdf(d)
            Env(d, pages=100, fail=()).run([pdf, "--pages", "1-10,20-30", "--keep-cache", "-o", str(Path(d) / "a.md")])
            keys1 = {p.name for p in (Path(d) / "cache").iterdir()}
            Env(d, pages=100).run([pdf, "--pages", "1-30", "--keep-cache", "-o", str(Path(d) / "b.md")])
            keys2 = {p.name for p in (Path(d) / "cache").iterdir()}
            self.assertEqual(len(keys2 - keys1), 1)
            env = Env(d, pages=100).run([pdf, "--pages", "20-30,1-10", "-o", str(Path(d) / "c.md")])
            self.assertEqual(env.slices, [])            # same normalised spec: fully cached
            self.assertIn("resuming", env.out.getvalue())

    def test_split_index_carries_the_spec(self):
        from test_pdf_to_md import BOOK
        with tempfile.TemporaryDirectory() as d:
            Env(d, pages=100, md=BOOK).run([make_pdf(d), "--pages", "3-9,20", "--split-chapters", "--no-backup"])
            idx = next(Path(d).glob("*/00 Index.md")).read_text()
            self.assertIn('pages: "3-9,20"\n', idx)

    def test_info_accepts_a_list(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(backend, "rate_file", lambda: Path(d) / "r.json"):
            buf = io.StringIO()
            with mock.patch.object(m, "page_count", lambda p: 200), contextlib.redirect_stdout(buf):
                backend.main(["info", "--json", make_pdf(d), "--pages", "10-40,60-80,95"])
            self.assertEqual(json.loads(buf.getvalue())["pages"], 31 + 21 + 1)


if __name__ == "__main__":
    unittest.main()
