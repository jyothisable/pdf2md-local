"""The PDF's own link annotations: reading them (pypdf) and applying them to converted pages (offline)."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pdf2md_local import pdf_links  # noqa: E402
from pdf2md_local.xref import add_anchors  # noqa: E402
from test_cli import Env  # noqa: E402

HAS_PYPDF = importlib.util.find_spec("pypdf") is not None


def build_pdf(pages, named=None):
    """A minimal PDF. pages: [{"words": [(x, y, text)], "links": [{"rect", "dest": (page_no, top) | name, "uri"}]}].
    Every word is its own text object so rectangles are easy to place."""
    objs: list[bytes] = []

    def add(b):
        objs.append(b if isinstance(b, bytes) else b.encode("latin-1"))
        return len(objs)

    cat, pages_id, font = add(b""), add(b""), add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids = []
    first_page = len(objs) + 1
    # reserve ids: for each page: page, contents, annots...
    layout = []
    nid = first_page
    for pg in pages:
        ids = {"page": nid, "content": nid + 1, "annots": [nid + 2 + i for i in range(len(pg.get("links", [])))]}
        nid += 2 + len(ids["annots"])
        layout.append(ids)
    page_ids = [l["page"] for l in layout]
    for pg, ids in zip(pages, layout):
        annots = " ".join(f"{a} 0 R" for a in ids["annots"])
        add(f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] /Contents {ids['content']} 0 R "
            f"/Resources << /Font << /F1 {font} 0 R >> >> /Annots [{annots}] >>")
        stream = "".join(f"BT /F1 12 Tf {x} {y} Td ({t}) Tj ET\n" for x, y, t in pg.get("words", []))
        add(f"<< /Length {len(stream)} >>\nstream\n{stream}endstream")
        for ln in pg.get("links", []):
            r = " ".join(str(v) for v in ln["rect"])
            if "uri" in ln:
                add(f"<< /Type /Annot /Subtype /Link /Rect [{r}] /Border [0 0 0] /A << /S /URI /URI ({ln['uri']}) >> >>")
            elif isinstance(ln["dest"], str):
                add(f"<< /Type /Annot /Subtype /Link /Rect [{r}] /Border [0 0 0] /A << /S /GoTo /D ({ln['dest']}) >> >>")
            else:
                pn, top = ln["dest"]
                add(f"<< /Type /Annot /Subtype /Link /Rect [{r}] /Border [0 0 0] /Dest [{page_ids[pn - 1]} 0 R /XYZ 0 {top} null] >>")
    kids = " ".join(f"{i} 0 R" for i in page_ids)
    objs[pages_id - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
    extra = ""
    if named:
        names = " ".join(f"({k}) [{page_ids[pn - 1]} 0 R /XYZ 0 {top} null]" for k, (pn, top) in sorted(named.items()))
        extra = f" /Names << /Dests << /Names [{names}] >> >>"
    objs[cat - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R{extra} >>".encode()
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for o in offs:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root {cat} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def word(x, y, t):
    return (x, y, t)


def rect_of(x, y, t):
    return (x - 0.5, y - 3, x + 6.0 * len(t) + 0.5, y + 10)


def sample_pdf(path):
    words1 = [word(72, 700, "See"), word(110, 700, "Figure"), word(160, 700, "2.1"), word(72, 680, "and"),
              word(110, 680, "equation"), word(180, 680, "(3.2)"), word(72, 660, "and"), word(110, 660, "[12]"),
              word(72, 640, "http://example.org/x"), word(72, 620, "Section"), word(130, 620, "4.1")]
    words2 = [word(72, 700, "Figure"), word(120, 700, "2.1"), word(150, 700, "A"), word(160, 700, "cat"),
              word(72, 600, "4.1"), word(100, 600, "Training"), word(150, 600, "methods"),
              word(72, 500, "[12]"), word(110, 500, "Smith"), word(150, 500, "J.,"), word(180, 500, "A"),
              word(190, 500, "paper"), word(230, 500, "about"), word(270, 500, "things")]
    links = [{"rect": rect_of(160, 700, "2.1"), "dest": (2, 712)},
             {"rect": rect_of(180, 680, "(3.2)"), "dest": "eq32"},
             {"rect": rect_of(110, 660, "[12]"), "dest": (2, 512)},
             {"rect": rect_of(72, 640, "http://example.org/x"), "uri": "http://example.org/x"},
             {"rect": rect_of(130, 620, "4.1"), "dest": (2, 612)}]
    Path(path).write_bytes(build_pdf([{"words": words1, "links": links}, {"words": words2}], named={"eq32": (2, 300)}))


@unittest.skipUnless(HAS_PYPDF, "pypdf optional")
class ReadLinks(unittest.TestCase):
    def test_reads_internal_named_and_url_links(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.pdf"
            sample_pdf(p)
            got = pdf_links.read_links(p, {1, 2})
            by = {x["text"]: x for x in got["internal"]}
            self.assertEqual(sorted(by), ["(3.2)", "2.1", "4.1", "[12]"])
            self.assertEqual((by["2.1"]["dest_page"], by["2.1"]["before"]), (2, "See Figure"))
            self.assertTrue(by["2.1"]["dest_text"].startswith("Figure 2.1"))
            self.assertEqual(by["(3.2)"]["dest_page"], 2)           # named destination
            self.assertEqual(by["[12]"]["dest_text"].split()[0], "[12]")
            self.assertTrue(by["4.1"]["dest_text"].startswith("4.1 Training"))
            self.assertEqual([(u["text"], u["url"]) for u in got["url"]], [("http://example.org/x", "http://example.org/x")])

    def test_only_selected_pages(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.pdf"
            sample_pdf(p)
            self.assertEqual(pdf_links.read_links(p, {2}), {"internal": [], "url": []})
            got = pdf_links.read_links(p, {1})   # destinations on page 2 are not converted
            self.assertEqual(got["internal"], [])
            self.assertEqual(len(got["url"]), 1)

    def test_no_annotations_and_garbage(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "plain.pdf"
            p.write_bytes(build_pdf([{"words": [(72, 700, "Hello")]}]))
            self.assertEqual(pdf_links.read_links(p), {"internal": [], "url": []})
            g = Path(d) / "bad.pdf"
            g.write_bytes(b"%PDF-1.4 fake")
            self.assertEqual(pdf_links.read_links(g), {"internal": [], "url": []})
            self.assertEqual(pdf_links.read_links(Path(d) / "missing.pdf"), {"internal": [], "url": []})

    def test_pypdf_failure_degrades(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.pdf"
            sample_pdf(p)
            with mock.patch.dict(sys.modules, {"pypdf": None}):
                self.assertEqual(pdf_links.read_links(p), {"internal": [], "url": []})
            with mock.patch("pypdf.PdfReader", side_effect=RuntimeError("boom")):
                self.assertEqual(pdf_links.read_links(p), {"internal": [], "url": []})


def link(page, text, before, dest_page, dest_text, y=700, x=100, **kw):
    return {"page": page, "rect": (x, y - 3, x + 20, y + 10), "text": text, "before": before, "after": "",
            "dest_page": dest_page, "dest_y": 500.0, "dest_text": dest_text, **kw}


def url(page, text, u, y=700, x=100):
    return {"page": page, "rect": (x, y - 3, x + 20, y + 10), "text": text, "before": "", "after": "", "url": u}


def doc(plain=False):
    notes = [{"file": "Book", "body": (
        "<!-- pdf2md:page=1 -->\n\n# 1 Intro\n\nAs shown in Figure 2.1 the cat sits, see also 4.1 and [12] or $x [12]$.\n\n"
        "Visit http://example.org/x today, and Section 4.1 again; contact us.\n\n"
        "<!-- pdf2md:page=2 -->\n\nFigure 2.1 A cat on a mat.\n\n## 4.1 Training methods\n\ntext here\n\n"
        "<!-- pdf2md:page=3 -->\n\n[12] Smith J., A paper about things. Journal, 2020.\n\n- item one about bananas\n- item two\n")}]
    return notes


class ApplyLinks(unittest.TestCase):
    def run_links(self, items, plain=False, notes=None, urls=()):
        notes = notes or doc(plain)
        reg = add_anchors(notes, plain)
        counts = pdf_links.apply_links(notes, {"internal": items, "url": list(urls)}, plain, reg)
        return notes[0]["body"], counts

    def test_figure_reference_uses_existing_anchor(self):
        for plain, want in ((False, "Figure [[#^fig-2-1|2.1]] the cat"), (True, "Figure [2.1](#fig-2-1) the cat")):
            body, c = self.run_links([link(1, "2.1", "As shown in Figure", 2, "Figure 2.1 A cat on a mat.")], plain)
            self.assertIn(want, body)
            self.assertEqual(c, {"pdf_links": 1, "url_links": 0})

    def test_heading_target_obsidian_only(self):
        item = link(1, "4.1", "see also", 2, "4.1 Training methods", x=200)
        body, c = self.run_links([item])
        self.assertIn("see also [[#4.1 Training methods|4.1]] and", body)
        self.assertEqual(c["pdf_links"], 1)
        body, c = self.run_links([item], plain=True)
        self.assertNotIn("](#", body.split("Figure 2.1 A cat")[0].split("see also")[1].split("[12]")[0])
        self.assertEqual(c["pdf_links"], 0)

    def test_bibliography_entry_gets_block_id(self):
        item = link(1, "[12]", "and", 3, "[12] Smith J., A paper about things.")
        body, c = self.run_links([item])
        self.assertEqual(c["pdf_links"], 1)
        self.assertIn("and [\\[12\\]](#^p3-12-smith-j) or $x [12]$", body)
        self.assertIn("Journal, 2020. ^p3-12-smith-j", body)
        body, c = self.run_links([item], plain=True)
        self.assertIn("[\\[12\\]](#p3-12-smith-j)", body)
        self.assertIn('<a id="p3-12-smith-j"></a>\n\n[12] Smith J.', body)

    def test_list_item_target(self):
        item = link(1, "also 4.1", "see", 3, "item one about bananas", x=200)
        item["text"], item["before"] = "cat sits", "the"
        body, _ = self.run_links([item])
        self.assertIn("- item one about bananas ^p3-item-one-about", body)
        self.assertIn("[[#^p3-item-one-about|cat sits]]", body)

    def test_text_occurring_twice_uses_reading_order(self):
        base = "<!-- pdf2md:page=1 -->\n\nsee 2.1 and again 2.1 here\n\n<!-- pdf2md:page=2 -->\n\nFigure 2.1 A\n\nFigure 2.2 B\n"
        notes = [{"file": "B", "body": base}]
        reg = add_anchors(notes, False)
        a = link(1, "2.1", "see", 2, "Figure 2.1 A", x=100)
        b = link(1, "2.1", "again", 2, "Figure 2.1 A", x=300)
        pdf_links.apply_links(notes, {"internal": [b, a], "url": []}, False, reg)
        self.assertIn("see [[#^fig-2-1|2.1]] and again [[#^fig-2-1|2.1]] here", notes[0]["body"])
        # one annotation only: the context picks the right occurrence
        notes = [{"file": "B", "body": base}]
        reg = add_anchors(notes, False)
        pdf_links.apply_links(notes, {"internal": [b], "url": []}, False, reg)
        self.assertIn("see 2.1 and again [[#^fig-2-1|2.1]] here", notes[0]["body"])
        # without context the k-th identical text goes to the k-th annotation in reading order
        notes = [{"file": "B", "body": base}]
        reg = add_anchors(notes, False)
        pdf_links.apply_links(notes, {"internal": [{**a, "before": ""}], "url": []}, False, reg)
        self.assertIn("see [[#^fig-2-1|2.1]] and again 2.1 here", notes[0]["body"])

    def test_missing_text_and_outside_page_are_skipped(self):
        items = [link(1, "7.7", "see", 2, "Figure 2.1 A cat on a mat."), link(9, "2.1", "", 2, "x"),
                 link(1, "", "", 2, "x")]
        body, c = self.run_links(items)
        self.assertEqual(c["pdf_links"], 0)
        self.assertEqual(body, doc()[0]["body"].replace("Figure 2.1 A cat on a mat.", "Figure 2.1 A cat on a mat. ^fig-2-1"
                                                        ).replace("Figure 2.1 A cat on a mat. ^fig-2-1", "Figure 2.1 A cat on a mat. ^fig-2-1"))

    def test_math_code_and_existing_links_not_touched(self):
        notes = [{"file": "B", "body": "<!-- pdf2md:page=1 -->\n\n$a 2.1$ `2.1` [t 2.1](u) [[n|2.1]] 2.1\n\n"
                                       "<!-- pdf2md:page=2 -->\n\nFigure 2.1 A\n"}]
        reg = add_anchors(notes, False)
        c = pdf_links.apply_links(notes, {"internal": [link(1, "2.1", "", 2, "Figure 2.1 A")], "url": []}, False, reg)
        self.assertEqual(c["pdf_links"], 1)
        self.assertIn("$a 2.1$ `2.1` [t 2.1](u) [[n|2.1]] [[#^fig-2-1|2.1]]", notes[0]["body"])

    def test_urls_both_flavours_and_scheme_filter(self):
        for plain in (False, True):
            notes = doc(plain)
            n = pdf_links.apply_links(notes, {"internal": [], "url": [
                url(1, "http://example.org/x", "http://example.org/x"),
                url(1, "Visit", "javascript:alert(1)"), url(1, "today", "file:///etc/passwd"),
                url(1, "contact", "mailto:a@b.org")]}, plain, {})
            self.assertEqual(n, {"pdf_links": 0, "url_links": 2})
            body = notes[0]["body"]
            self.assertIn("Visit [http://example.org/x](http://example.org/x) today", body)
            self.assertIn("[contact](mailto:a@b.org)", body)
            self.assertNotIn("javascript", body)
            self.assertNotIn("file:", body)
            again = pdf_links.apply_links(notes, {"internal": [], "url": [url(1, "http://example.org/x", "http://example.org/x")]},
                                          plain, {})
            self.assertEqual(again["url_links"], 0)

    def test_idempotent_and_counts(self):
        items = [link(1, "2.1", "As shown in Figure", 2, "Figure 2.1 A cat on a mat."),
                 link(1, "[12]", "and", 3, "[12] Smith J., A paper about things.")]
        notes = doc()
        reg = add_anchors(notes, False)
        first = pdf_links.apply_links(notes, {"internal": items, "url": []}, False, reg)
        snap = notes[0]["body"]
        second = pdf_links.apply_links(notes, {"internal": items, "url": []}, False, reg)
        self.assertEqual(first["pdf_links"], 2)
        self.assertEqual(second["pdf_links"], 0)
        self.assertEqual(notes[0]["body"], snap)
        self.assertEqual(snap.count("^p3-12-smith-j"), 2)   # one anchor, one link

    def test_pages_across_split_notes(self):
        notes = [{"file": "01 A", "body": "<!-- pdf2md:page=1 -->\n\nSee 2.1 here.\n"},
                 {"file": "02 B", "body": "<!-- pdf2md:page=2 -->\n\nFigure 2.1 A cat.\n"}]
        reg = add_anchors(notes, False)
        pdf_links.apply_links(notes, {"internal": [link(1, "2.1", "See", 2, "Figure 2.1 A cat.")], "url": []}, False, reg)
        self.assertIn("See [[02 B#^fig-2-1|2.1]] here.", notes[0]["body"])
        notes = [{"file": "01 A", "body": "<!-- pdf2md:page=1 -->\n\nSee 2.1 here.\n"},
                 {"file": "02 B", "body": "<!-- pdf2md:page=2 -->\n\nFigure 2.1 A cat.\n"}]
        reg = add_anchors(notes, True)
        pdf_links.apply_links(notes, {"internal": [link(1, "2.1", "See", 2, "Figure 2.1 A cat.")], "url": []}, True, reg)
        self.assertIn("See [2.1](02%20B.md#fig-2-1) here.", notes[0]["body"])

    def test_classify(self):
        self.assertEqual(pdf_links.classify("see Figure", "2.1"), "fig-2-1")
        self.assertEqual(pdf_links.classify("", "(Figure 1-3)"), "fig-1-3")
        self.assertEqual(pdf_links.classify("in equation", "3.2"), "eq-3-2")
        self.assertEqual(pdf_links.classify("see", "(3.2)"), "eq-3-2")
        self.assertEqual(pdf_links.classify("by Theorem", "2.1"), "thm-2-1")
        self.assertIsNone(pdf_links.classify("see", "Smith"))


PRECISE = ("<!-- pdf2md:page=1 -->\n\nThe function in figure 3.3 shows where the line goes (see equation 4.3). Look at figures 3.13c–d and "
           "Section 3.4, then problem 3.11? Also (3.11) and the rest.\n\nWe say the unit has the family of functions.\n\n"
           "<!-- pdf2md:page=2 -->\n\nFigure 3.3 Computation for a shallow network.\n\nFigure 3.13 Activation functions.\n\n"
           "Figure 3.11 Something.\n\n$$\ny = x \\tag{4.3}\n$$\n\n## 3.4 Shallow networks\n\nbody\n\n"
           "Problem 3.11 What kind of mapping from input.\n")


class Precision(unittest.TestCase):
    def apply(self, items, plain=False, doc_text=PRECISE):
        notes = [{"file": "B", "body": doc_text}]
        reg = add_anchors(notes, plain)
        c = pdf_links.apply_links(notes, {"internal": items, "url": []}, plain, reg)
        return notes[0]["body"], c["pdf_links"]

    def test_word_guess_off_by_one_links_the_number_next_to_it(self):
        # the guessed text is the word after the number: link the number, never the word
        item = link(1, "shows", "in figure 3.3", 2, "Figure 3.3 Computation for a shallow network.")
        item["after"] = "where the line"
        body, n = self.apply([item])
        self.assertIn("figure [[#^fig-3-3|3.3]] shows where", body)
        self.assertNotIn("|shows]]", body)
        self.assertEqual(n, 1)
        item = link(1, "where", "figure 3.3 shows", 2, "Figure 3.3 Computation for a shallow network.")
        body, _ = self.apply([item])
        self.assertNotIn("|where]]", body)

    def test_ordinary_word_without_number_is_never_linked_to_an_anchor(self):
        for w in ("function", "where", "has", "which", "unit"):
            body, n = self.apply([link(1, w, "", 2, "Figure 3.3 Computation for a shallow network.")])
            self.assertEqual(n, 0, w)
            self.assertNotIn(f"|{w}]]", body)

    def test_number_forms_with_letters_and_ranges(self):
        item = link(1, "3.13c–d", "Look at figures", 2, "Figure 3.13 Activation functions.")
        body, n = self.apply([item])
        self.assertIn("figures [[#^fig-3-13|3.13c–d]] and", body)
        self.assertEqual(n, 1)

    def test_kind_mismatch_never_links_other_kind(self):
        # "figure 4.3" must not go to eq-4-3 even though only the equation exists
        doc_text = PRECISE.replace("(see equation 4.3)", "(see figure 4.3)")
        item = link(1, "4.3", "(see figure", 2, "y = x")
        body, n = self.apply([item], doc_text=doc_text)
        self.assertNotIn("^eq-4-3|4.3", body)
        self.assertEqual(n, 0)
        # the right word does link
        body, n = self.apply([link(1, "4.3", "(see equation", 2, "y = x")])
        self.assertIn("equation [[#^eq-4-3|4.3]])", body)

    def test_occurrence_after_a_different_kind_word_is_skipped(self):
        doc_text = ("<!-- pdf2md:page=1 -->\n\nFolded as in figure 4.3 here. Later substitute into equation 4.3 now.\n\n"
                    "<!-- pdf2md:page=2 -->\n\n$$\ny = x \\tag{4.3}\n$$\n")
        # the context "(equation" does not occur verbatim, so the first "4.3" would be next in line: it follows "figure"
        item = link(1, "4.3", "(equation", 2, "y = x")
        body, n = self.apply([item], doc_text=doc_text)
        self.assertEqual(n, 1)
        self.assertIn("figure 4.3 here", body)
        self.assertIn("equation [[#^eq-4-3|4.3]] now", body)

    def test_kind_word_is_part_of_label_when_in_the_guess(self):
        body, _ = self.apply([link(1, "figure 3.3", "in", 2, "Figure 3.3 Computation for a shallow network.")])
        self.assertIn("in [[#^fig-3-3|figure 3.3]] shows", body)

    def test_trailing_punctuation_outside_label(self):
        item = link(1, "3.11?", "then problem", 2, "Problem 3.11 What kind of mapping from input.")
        body, n = self.apply([item])
        self.assertEqual(n, 1)
        self.assertRegex(body, r"problem \[\[#\^p2-problem-3-11\|3\.11\]\]\? Also")
        item = link(1, "(3.11)", "Also", 2, "Figure 3.11 Something.")
        body, _ = self.apply([item])
        self.assertNotIn("|(3.11)]]", body)

    def test_single_stopwords_and_weak_phrases_are_rejected_for_paragraph_targets(self):
        for t in ("and", "to", "the", "of the", "which is"):
            body, n = self.apply([link(1, t, "", 3, "Problem 3.11 What kind of mapping from input.")])
            self.assertEqual(n, 0, t)
        self.assertTrue(pdf_links.useful_text("shallow networks"))
        self.assertTrue(pdf_links.useful_text("[12]"))
        self.assertFalse(pdf_links.useful_text("function"))
        self.assertFalse(pdf_links.useful_text("of the"))

    def test_phrase_target_by_snippet_still_works(self):
        body, n = self.apply([link(1, "shallow network", "a", 2, "3.4 Shallow networks")], doc_text=PRECISE.replace(
            "and the rest.", "and a shallow network here."))
        self.assertIn("a [[#3.4 Shallow networks|shallow network]] here", body)

    def test_url_label_drops_shifted_stopwords(self):
        notes = [{"file": "B", "body": "<!-- pdf2md:page=1 -->\n\nsee the interactive figure (the notebook) here\n"}]
        n = pdf_links.apply_links(notes, {"internal": [], "url": [url(1, "the interactive figure (the", "https://x.org/y")]}, False, {})
        self.assertEqual(n["url_links"], 1)
        self.assertIn("see the [interactive figure](https://x.org/y) (the notebook)", notes[0]["body"])

    def test_trim_label(self):
        for raw, want in [("3.11?", "3.11"), ("(3.2)", "(3.2)"), ("(see", "see"), ("3.2).", "3.2"), ("[12]", "[12]"),
                          ("3.8d–", "3.8d"), ("“quoted”", "quoted")]:
            self.assertEqual(pdf_links.trim_label(raw), want, raw)


@unittest.skipUnless(HAS_PYPDF, "pypdf optional")
class EndToEnd(unittest.TestCase):
    MD = ("<!-- pdf2md:page=1 -->\n\n# 1 Intro\n\nSee Figure 2.1 and equation (3.2) and [12] at http://example.org/x and Section 4.1.\n\n"
          "<!-- pdf2md:page=2 -->\n\nFigure 2.1 A cat.\n\n$$\nx = 1 \\tag{3.2}\n$$\n\n## 4.1 Training methods\n\ntext\n\n"
          "[12] Smith J., A paper about things. Journal, 2020.\n\n# 2 More\n\nOther.\n")

    def convert(self, d, extra, plain, md=None):
        pdf = Path(d) / "Book.pdf"
        sample_pdf(pdf)
        flavor = "plain" if plain else "obsidian"
        env = Env(d, pages=2, md=md or self.MD).run([str(pdf), "-o", str(Path(d) / "Book.md"), "--no-backup", "--flavor", flavor, *extra])
        line = [x for x in env.out.getvalue().splitlines() if x.startswith("RESULT ")][0]
        return json.loads(line[7:]), (Path(d) / "Book.md").read_text(), env.out.getvalue()

    def test_obsidian_links_everything(self):
        with tempfile.TemporaryDirectory() as d:
            r, text, out = self.convert(d, [], False)
            self.assertEqual(r["pdf_links"], 4)
            self.assertEqual(r["url_links"], 1)
            self.assertIn("[[#^fig-2-1|2.1]]", text)
            self.assertIn("[[#4.1 Training methods|4.1]]", text)
            self.assertIn("[\\[12\\]](#^p2-12-smith-j)", text)
            self.assertIn("[http://example.org/x](http://example.org/x)", text)
            self.assertNotIn("pdf2md:page", text)
            self.assertIn("PDF link(s)", out)

    def test_off_means_no_pdf_links_at_all(self):
        with tempfile.TemporaryDirectory() as d:
            r, text, out = self.convert(d, ["--pdf-links", "off"], False)
            self.assertEqual((r["pdf_links"], r["url_links"]), (0, 0))
            self.assertNotIn("](http://example.org", text)

    def test_plain_auto_links_only_urls(self):
        with tempfile.TemporaryDirectory() as d:
            r, text, out = self.convert(d, [], True)
            self.assertEqual((r["pdf_links"], r["url_links"]), (0, 1))
            self.assertIn("[http://example.org/x](http://example.org/x)", text)
            self.assertNotIn("<a id", text)

    def test_plain_on(self):
        with tempfile.TemporaryDirectory() as d:
            r, text, _ = self.convert(d, ["--cross-links", "on", "--callouts", "on"], True)
            self.assertGreaterEqual(r["pdf_links"], 1)
            self.assertIn('<a id="fig-2-1"></a>', text)

    def test_page_markers_kept(self):
        with tempfile.TemporaryDirectory() as d:
            r, text, _ = self.convert(d, ["--page-markers"], False)
            self.assertIn("<!-- pdf2md:page=2 -->", text)
            self.assertEqual(r["pdf_links"], 4)

    def test_fake_pdf_behaves_as_before(self):
        from test_cli import make_pdf
        outs = []
        for extra in ([], ["--pdf-links", "off"]):
            with tempfile.TemporaryDirectory() as d2:
                Env(d2, pages=2, md=self.MD).run([make_pdf(d2), "--no-backup", "--flavor", "obsidian", *extra])
                outs.append((Path(d2) / "paper.md").read_text())
        self.assertEqual(outs[0], outs[1])
        self.assertNotIn("](http://", outs[0])

    def test_cache_key_ignores_pdf_links_flag(self):
        from pdf2md_local import convert as m
        keys = []
        for extra in ([], ["--pdf-links", "off"], ["--pdf-links", "on"]):
            with tempfile.TemporaryDirectory() as d:
                seen = []
                orig = m.run_key
                m.run_key = lambda pdf, *parts, _o=orig, _s=seen: (_s.append(parts), _o(pdf, *parts))[1]
                try:
                    pdf = Path(d) / "Book.pdf"
                    sample_pdf(pdf)
                    Env(d, pages=2, md=self.MD).run([str(pdf), "--no-backup", "--flavor", "obsidian", *extra])
                finally:
                    m.run_key = orig
                keys.append(seen[0])
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(keys[0], keys[2])


if __name__ == "__main__":
    unittest.main()
