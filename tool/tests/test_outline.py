"""Bookmark repair must not require an OCR model or change body content."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('pdf2md_outline', Path(__file__).resolve().parents[1] / 'pdf_to_md.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class OutlineTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('pypdf'), 'pypdf optional in lightweight test environment')
    def test_read_actual_pdf_destinations(self):
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'outline.pdf'
            writer = PdfWriter()
            for _ in range(5):
                writer.add_blank_page(width=100, height=100)
            parent = writer.add_outline_item('1 Chapter', 1)
            writer.add_outline_item('1.1 Topic', 2, parent=parent)
            writer.write(path)
            self.assertEqual(m.read_pdf_outline(path), [
                {'level': 1, 'title': '1 Chapter', 'page': 2},
                {'level': 2, 'title': '1.1 Topic', 'page': 3}])

    def test_wrappers_broken_duplicate_tree_and_nonmonotonic_destinations(self):
        raw = [[1, 'Ch01_7P', 5], [2, '1 First', 5], [3, '1.1 Section', 6],
               [1, 'Ch02_7P', 10], [2, '2 Second', 10],
               [1, 'embedded.pdf', -1], [2, '1 First', -1], [2, 'Index', 1]]
        self.assertEqual(m.normalize_pdf_outline(raw, 20), [
            {'level': 1, 'title': '1 First', 'page': 5},
            {'level': 2, 'title': '1.1 Section', 'page': 6},
            {'level': 1, 'title': '2 Second', 'page': 10}])

    def test_split_title_keeps_page_link_and_math(self):
        text = '# First\n\n[[book.pdf#page=5]]\n\n## 1.1 A first look:\n\n[[book.pdf#page=6]]\n\n## A paradigm shift\n\n$$\nx^2\n$$\n'
        outline = [{'level': 1, 'title': '1 First', 'page': 5},
                   {'level': 2, 'title': '1.1 A first look: A paradigm shift', 'page': 6}]
        out, r = m.repair_pdf_headings(text, outline)
        self.assertIn('# 1 First', out)
        self.assertIn('## 1.1 A first look: A paradigm shift', out)
        self.assertEqual(out.count('[[book.pdf#page='), 2)
        self.assertIn('$$\nx^2\n$$', out)
        self.assertEqual(r['matched'], 2)
        self.assertEqual(m.repair_pdf_headings(out, outline)[0], out)

    def test_same_title_different_pages(self):
        text = '<!-- pdf2md:page=2 -->\n## Summary\n<!-- pdf2md:page=20 -->\n### Summary\n'
        out, r = m.repair_pdf_headings(text, [{'level': 2, 'title': 'Summary', 'page': 20}])
        self.assertIn('page=2 -->\n## Summary', out)
        self.assertIn('page=20 -->\n## Summary', out)
        self.assertEqual(r['matched'], 1)

    def test_ambiguous_same_page_is_untouched(self):
        text = '<!-- pdf2md:page=2 -->\n## Overview\n\n## Overview'
        out, r = m.repair_pdf_headings(text, [{'level': 2, 'title': 'Overview', 'page': 2}])
        self.assertEqual(out, text)
        self.assertEqual(len(r['ambiguous']), 1)

    def test_code_math_and_missing_page_anchors_are_untouched(self):
        text = '<!-- pdf2md:page=2 -->\n```python\n# Overview\n```\n$$\n# Overview\n$$'
        out, r = m.repair_pdf_headings(text, [{'level': 1, 'title': 'Overview', 'page': 2}])
        self.assertEqual(out, text)
        self.assertEqual(r['matched'], 0)
        self.assertEqual(m.repair_pdf_headings('# Overview', [{'level': 1, 'title': 'Overview', 'page': 2}])[0], '# Overview')

    def test_number_conflict_does_not_reassign_a_section(self):
        text = '<!-- pdf2md:page=2 -->\n## 2.1 Overview'
        out, r = m.repair_pdf_headings(text, [{'level': 2, 'title': '1.1 Overview', 'page': 2}])
        self.assertEqual(out, text)
        self.assertEqual(r['matched'], 0)

    def test_numbered_subsections_survive_shallow_bookmarks(self):
        text = '<!-- pdf2md:page=2 -->\n# 1 Chapter\n\n## 1.1 Overview\n\n### 1.1.1 Details\n\n# Local callout\n\n```\n# Keep code\n```'
        outline = [{'level': 1, 'title': 'Chapter', 'page': 2}, {'level': 2, 'title': 'Overview', 'page': 2}]
        out, _ = m.repair_pdf_headings(text, outline)
        self.assertIn('## 1.1 Overview', out)
        self.assertIn('### 1.1.1 Details', out)
        self.assertIn('#### Local callout', out)
        self.assertIn('```\n# Keep code\n```', out)
        self.assertEqual(m.repair_pdf_headings(out, outline)[0], out)

    def test_plain_chapter_title_enables_splitting(self):
        text = '<!-- pdf2md:page=2 -->\nFirst chapter\n\nText\n<!-- pdf2md:page=4 -->\nSecond chapter\n\nText'
        outline = [{'level': 1, 'title': '1 First chapter', 'page': 2},
                   {'level': 1, 'title': '2 Second chapter', 'page': 4}]
        out, r = m.repair_pdf_headings(text, outline)
        self.assertEqual(r['matched'], 2)
        self.assertEqual(len(m.split_book(out, 'Book')), 3)  # front page marker, two chapters

    def test_fuzzy_bookmark_does_not_rewrite_spelling(self):
        text = '<!-- pdf2md:page=2 -->\n#### 1.1 Graphical and algebraic view'
        out, _ = m.repair_pdf_headings(text, [{'level': 2, 'title': '1.1 Graphical and algebraical view', 'page': 2}])
        self.assertIn('## 1.1 Graphical and algebraic view', out)
        self.assertNotIn('algebraical', out)

    def test_end_sections_are_not_nested_under_summary(self):
        text = '<!-- pdf2md:page=2 -->\n# 1 Chapter\n## 1.1 Topic\n## Summary\n## Notes\n## Problems'
        outline = [{'level': 1, 'title': 'Chapter', 'page': 2}, {'level': 2, 'title': 'Summary', 'page': 2}]
        out, _ = m.repair_pdf_headings(text, outline)
        self.assertEqual(out, text)

    def test_toc_title_at_wrong_page_is_not_a_chapter(self):
        text = '<!-- pdf2md:page=1 -->\n# 1 Chapter\n\n<!-- pdf2md:page=10 -->\nChapter\n\nBody'
        out, r = m.repair_pdf_headings(text, [{'level': 1, 'title': '1 Chapter', 'page': 10}])
        self.assertEqual(r['matched'], 1)
        self.assertIn('page=1 -->\n# 1 Chapter', out)
        self.assertIn('page=10 -->\n# 1 Chapter', out)

    def test_appendix_wrapper_retains_semantic_section(self):
        raw = [[1, 'App_7P', 10], [2, 'A ', 10], [3, 'A.1 Topic', 11],
               [1, 'Notations_7P', 15], [1, 'Index_2P', 18], [2, 'Index', 18]]
        out = m.normalize_pdf_outline(raw,20)
        self.assertEqual([x['title'] for x in out], ['Appendix','A.1 Topic','Notations','Index'])
        self.assertEqual([x['level'] for x in out], [1,2,1,1])

    def test_repair_can_be_used_without_an_outline(self):
        text = '# Chapter\n\n## Topic\n\nBody'
        self.assertEqual(m.repair_pdf_headings(text,[])[0], text)


if __name__ == '__main__':
    unittest.main()
