"""End-to-end wiring of the pdf2md CLI with the OCR worker and model server replaced."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pdf2md_local import backend, convert as m  # noqa: E402
from test_pdf_to_md import BOOK  # noqa: E402


class Env:
    """Patches the worker, the server guard and the readiness check; records what the CLI asked for."""

    def __init__(self, d, pages=100, md="Intro text.\n\n$$\nE = mc^2\n$$\n", fail=()):
        self.d, self.pages, self.md, self.fail = Path(d), pages, md, set(fail)
        self.slices, self.guards, self.backups, self.out = [], [], [], io.StringIO()

    def fake_slice(self, pdf, rng, assets, server, page_links, on_page=None, scale=2.5):
        self.slices.append((pdf.name, rng, server, page_links, scale))
        if pdf.name in self.fail:
            sys.exit("boom")
        assets.mkdir(parents=True, exist_ok=True)
        (assets / "image_000000_abc.png").write_bytes(b"png")
        for i in range(1, rng[1] - rng[0] + 2):
            if on_page:
                on_page(i, rng[1] - rng[0] + 1)
        return self.md, m.new_stats()

    def run(self, argv):
        env = self

        class Guard:
            def __init__(self, url, command, wait):
                env.guards.append((url, command, wait))

            def __enter__(self):
                return self

            def __exit__(self, *a):
                env.guards.append("exit")

        with mock.patch.object(m, "paddle_slice", self.fake_slice), mock.patch.object(m, "ServerGuard", Guard), \
             mock.patch.object(backend, "require_ready", lambda: None), \
             mock.patch.object(m, "page_count", lambda p: self.pages), \
             mock.patch.object(m, "run_backup", lambda c: self.backups.append((c, list(self.d.glob("*.md"))))), \
             mock.patch.object(m, "CACHE_ROOT", self.d / "cache"), \
             contextlib.redirect_stdout(self.out):
            m.main(argv)
        return self

    def progress(self):
        return [l for l in self.out.getvalue().splitlines() if l.startswith("PROGRESS ")]


def make_pdf(d, name="paper.pdf"):
    p = Path(d) / name
    p.write_bytes(b"%PDF-1.4 fake")
    return str(p)


class SingleNoteTests(unittest.TestCase):
    def test_pipeline_wires_up(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            env = Env(d).run([make_pdf(d), "-o", str(out), "--flavor", "obsidian", "--pages", "10-60", "--pages-per-batch", "20"])
            self.assertEqual([s[1] for s in env.slices], [(10, 29), (30, 49), (50, 60)])
            md = next(out.glob("*.md")).read_text()
            self.assertIn("E = mc^2", md)
            self.assertIn('pages: "10-60"', md)
            self.assertEqual(sorted(p.name for p in out.glob("*_assets/*")), ["image_000000_abc.png"])
            self.assertEqual(list((Path(d) / "cache").glob("*")), [])       # checkpoint removed after success
            self.assertEqual(env.guards[-1], "exit")

    def test_hidden_page_markers_are_removed_unless_kept(self):
        md = "<!-- pdf2md:page=1 -->\n\n# 1 One\n\ntext\n\n<!-- pdf2md:page=2 -->\n\nmore\n"
        with tempfile.TemporaryDirectory() as d:
            Env(d, md=md).run([make_pdf(d)])
            text = (Path(d) / "paper.md").read_text()
            self.assertNotIn("pdf2md:page", text)
            self.assertIn("# 1 One\n\ntext\n\nmore", text)
        with tempfile.TemporaryDirectory() as d:
            Env(d, md=md).run([make_pdf(d), "--page-markers"])
            self.assertEqual((Path(d) / "paper.md").read_text().count("pdf2md:page="), 2)

    def test_math_heading_and_entity_fixes_applied(self):
        with tempfile.TemporaryDirectory() as d:
            Env(d, md="# 3.4 Title &amp; more\n\nSome \\(x\\) text.\n").run([make_pdf(d)])
            md = (Path(d) / "paper.md").read_text()
            self.assertIn("## 3.4 Title & more", md)
            self.assertIn("$x$", md)

    def test_server_options_reach_the_guard_and_worker(self):
        with tempfile.TemporaryDirectory() as d:
            env = Env(d).run([make_pdf(d), "--paddle-server", "http://h:9/v1", "--serve-command", "start-it",
                              "--serve-wait", "7", "--paddle-scale", "4"])
            self.assertEqual(env.guards, [("http://h:9/v1", "start-it", 7), "exit"])
            self.assertEqual(env.slices[0][2:], ("http://h:9/v1", False, 4.0))

    def test_default_slices_are_large(self):
        with tempfile.TemporaryDirectory() as d:
            env = Env(d, pages=300).run([make_pdf(d)])
            self.assertEqual([s[1] for s in env.slices], [(1, 120), (121, 240), (241, 300)])

    def test_progress_lines(self):
        with tempfile.TemporaryDirectory() as d:
            env = Env(d, pages=7).run([make_pdf(d), "--pages-per-batch", "3"])
            self.assertEqual([int(g.split()[1]) for g in env.progress()], [0, 1, 2, 3, 3, 4, 5, 6, 6, 7, 7])
            self.assertEqual(env.progress()[-1], "PROGRESS 7 7")
        with tempfile.TemporaryDirectory() as d:
            env = Env(d).run([make_pdf(d), "--pages", "11-30", "--pages-per-batch", "10"])
            self.assertEqual(env.progress()[0], "PROGRESS 0 20")
            self.assertEqual(env.progress()[-1], "PROGRESS 20 20")


class FlavorTests(unittest.TestCase):
    MD = "# 1 One\n\n[[paper.pdf#page=3|p. 3]]\n\nSee ![[image_000000_abc.png]].\n\n# 2 Two\n\nb\n"

    def test_plain_flavor_has_no_wikilinks(self):
        with tempfile.TemporaryDirectory() as d:
            Env(d, md=self.MD).run([make_pdf(d), "--flavor", "plain", "--page-links", "on"])
            text = (Path(d) / "paper.md").read_text()
            self.assertNotIn("[[", text)
            self.assertIn("![image_000000_abc](paper_assets/image_000000_abc.png)", text)
            self.assertIn("(paper.pdf#page=", text)

    def test_obsidian_flavor_keeps_embeds(self):
        with tempfile.TemporaryDirectory() as d:
            Env(d, md=self.MD).run([make_pdf(d), "--flavor", "obsidian"])
            self.assertIn("![[image_000000_abc.png]]", (Path(d) / "paper.md").read_text())

    def test_auto_follows_the_vault(self):
        with tempfile.TemporaryDirectory() as d:
            env = Env(d, md=self.MD).run([make_pdf(d)])
            self.assertFalse(env.slices[0][3])                                # not in a vault: no page links
            self.assertNotIn("[[", (Path(d) / "paper.md").read_text())
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".obsidian").mkdir()
            env = Env(d, md=self.MD).run([make_pdf(d), "--no-backup"])
            self.assertTrue(env.slices[0][3])
            self.assertIn("![[image_000000_abc.png]]", (Path(d) / "paper.md").read_text())

    def test_to_plain_handles_spaces_and_navigation(self):
        out = m.to_plain("![[a b.png]] [[01 Intro|Next]] [[My Book.pdf#page=3|p. 3]]", "assets", "../My Book.pdf")
        self.assertEqual(out, "![a b](assets/a%20b.png) [Next](01%20Intro.md) [p. 3](../My%20Book.pdf#page=3)")


class SplitTests(unittest.TestCase):
    def test_split_run_writes_a_folder_and_no_single_note(self):
        with tempfile.TemporaryDirectory() as d:
            Env(d, pages=10, md=BOOK.split("---\n", 1)[1]).run([make_pdf(d, "My Book.pdf"), "--split-chapters", "--flavor", "obsidian"])
            folder = Path(d) / "My Book"
            self.assertEqual(sorted(p.name for p in folder.glob("*.md")),
                             ["00 Front matter.md", "00 Index.md", "01 Intro.md", "02 Models and more.md",
                              "03 Wrap up.md", "90 Bibliography.md", "91 Index.md"])
            self.assertFalse((Path(d) / "My Book.md").exists())
            self.assertTrue((folder / "assets" / "image_000000_abc.png").exists())
            self.assertIn("[[01 Intro", (folder / "00 Index.md").read_text())
            self.assertIn("chapter: 1", (folder / "01 Intro.md").read_text())

    def test_plain_split_uses_relative_links(self):
        with tempfile.TemporaryDirectory() as d:
            Env(d, pages=10, md=BOOK.split("---\n", 1)[1]).run([make_pdf(d, "My Book.pdf"), "--split-chapters", "--flavor", "plain"])
            folder = Path(d) / "My Book"
            index = (folder / "00 Index.md").read_text()
            self.assertNotIn("[[", index)
            self.assertIn("(01%20Intro.md)", index)
            self.assertNotIn("[[", (folder / "01 Intro.md").read_text())

    def test_split_falls_back_to_single_note_without_chapters(self):
        with tempfile.TemporaryDirectory() as d:
            Env(d, pages=3, md="# Title\n\ntext\n").run([make_pdf(d), "--split-chapters"])
            self.assertTrue((Path(d) / "paper.md").exists())


class BatchTests(unittest.TestCase):
    def test_several_pdfs_are_converted_in_turn(self):
        with tempfile.TemporaryDirectory() as d:
            pdfs = [make_pdf(d, f"{n}.pdf") for n in "abc"]
            env = Env(d, pages=2).run(pdfs + ["-o", str(Path(d) / "out")])
            self.assertEqual([s[0] for s in env.slices], ["a.pdf", "b.pdf", "c.pdf"])
            self.assertEqual(sorted(p.name for p in (Path(d) / "out").glob("*.md")), ["a.md", "b.md", "c.md"])

    def test_one_failure_does_not_stop_the_batch_but_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = make_pdf(d, "a.pdf"), make_pdf(d, "b.pdf")
            with self.assertRaises(SystemExit) as cm:
                Env(d, pages=2, fail=["a.pdf"]).run([a, b])
            self.assertIn("1 of 2 PDF(s) failed", str(cm.exception.code))
            self.assertTrue((Path(d) / "b.md").exists())

    def test_batch_needs_a_folder_for_output_and_valid_pdfs(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = make_pdf(d, "a.pdf"), make_pdf(d, "b.pdf")
            with self.assertRaises(SystemExit) as cm:
                Env(d).run([a, b, "-o", str(Path(d) / "x.md")])
            self.assertIn("folder", str(cm.exception.code))
            with self.assertRaises(SystemExit) as cm:
                Env(d).run([a, str(Path(d) / "missing.pdf")])
            self.assertIn("not a PDF file", str(cm.exception.code))


class BackupHookTests(unittest.TestCase):
    def test_find_vault_root(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d) / "vault"
            (vault / ".obsidian").mkdir(parents=True)
            (vault / "Books" / "sub").mkdir(parents=True)
            self.assertEqual(m.find_vault_root(vault / "Books" / "sub"), vault.resolve())
            self.assertIsNone(m.find_vault_root(Path(d)))

    def test_backup_command_success_and_failure(self):
        m.run_backup(f"{sys.executable} -c \"print('{{\\\"archive\\\": \\\"/b/x.zip\\\"}}')\"")
        with self.assertRaises(SystemExit):
            m.run_backup(f"{sys.executable} -c \"import sys; sys.exit(3)\"")

    def test_backup_runs_only_inside_a_vault_and_before_writing(self):
        for in_vault, expect in ((True, 1), (False, 0)):
            with tempfile.TemporaryDirectory() as d:
                if in_vault:
                    (Path(d) / ".obsidian").mkdir()
                env = Env(d, pages=1).run([make_pdf(d, "a.pdf"), "--backup-command", "BACKUP"])
                self.assertEqual(len(env.backups), expect)
                if expect:
                    self.assertEqual(env.backups[0], ("BACKUP", []))        # ran before the note existed
                self.assertTrue((Path(d) / "a.md").exists())

    def test_no_backup_flag_wins(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".obsidian").mkdir()
            env = Env(d, pages=1).run([make_pdf(d, "a.pdf"), "--no-backup", "--backup-command", "X"])
            self.assertEqual(env.backups, [])


class BackendTests(unittest.TestCase):
    def test_missing_environment_stops_with_setup_instructions(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"PDF2MD_HOME": d, "MODEL_DIR": d}):
            with self.assertRaises(SystemExit) as cm:
                backend.require_ready()
            self.assertIn("pdf2md setup", str(cm.exception.code))

    def test_check_reports_json_and_exit_code(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"PDF2MD_HOME": d, "MODEL_DIR": d}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = backend.main(["check", "--json", "--server", "http://127.0.0.1:9/v1"])
            self.assertEqual(rc, 1)
            data = __import__("json").loads(buf.getvalue())
            self.assertFalse(data["ready"])
            self.assertEqual({c["id"] for c in data["checks"]}, {"worker", "model", "llama-server", "server", "gpu"})
            self.assertTrue(all(c["fix"] for c in data["checks"] if c["required"] and not c["ok"]))

    def test_no_install_or_download_outside_setup(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"PDF2MD_HOME": d, "MODEL_DIR": d}), \
             mock.patch.object(backend, "_run", side_effect=AssertionError("install")), \
             mock.patch.object(backend, "_download", side_effect=AssertionError("download")):
            with contextlib.redirect_stdout(io.StringIO()):
                backend.main(["check"])
            with self.assertRaises(SystemExit):
                backend.require_ready()


if __name__ == "__main__":
    unittest.main()


class StripScriptTests(unittest.TestCase):
    def test_script_removes_only_marker_lines_and_backs_up(self):
        import subprocess
        script = Path(__file__).resolve().parents[2] / "scripts" / "strip_page_markers.py"
        with tempfile.TemporaryDirectory() as d:
            note = Path(d) / "n.md"
            original = "<!-- pdf2md:page=3 -->\n\n# Title\n\ntext <!-- pdf2md:page=9 --> inline stays\n\n<!-- pdf2md:page=4 -->\nmore\n"
            note.write_text(original)
            args = [sys.executable, "-I", str(script), str(note), "--backup-dir", str(Path(d) / "bk")]
            subprocess.run(args, check=True, capture_output=True)
            self.assertEqual(note.read_text(), original)                    # dry run changes nothing
            subprocess.run(args + ["--apply"], check=True, capture_output=True)
            self.assertEqual(note.read_text(), "# Title\n\ntext <!-- pdf2md:page=9 --> inline stays\n\nmore\n")
            self.assertEqual((Path(d) / "bk" / "n.md").read_text(), original)
