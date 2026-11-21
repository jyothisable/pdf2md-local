"""Failed pages and retry, cancel, locking, YAML escaping, the RESULT line and --version (offline)."""
import contextlib
import io
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pdf2md_local import __version__, convert as m, notes  # noqa: E402
from test_cli import Env, make_pdf  # noqa: E402
from test_pdf_to_md import BOOK  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def result_of(env) -> dict:
    lines = [l for l in env.out.getvalue().splitlines() if l.startswith("RESULT ")]
    assert len(lines) == 1, lines
    return json.loads(lines[0][len("RESULT "):])


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class FailedPageTests(unittest.TestCase):
    PAGES = {1: "A text", 2: None, 3: "C text"}

    @classmethod
    def doc(cls, rng, failed_ok=False):
        out = []
        for p in range(rng[0], rng[1] + 1):
            body = cls.PAGES.get(p) or f"P{p} text"
            out.append(f"<!-- pdf2md:page={p} -->\n\n{body}" if body and (failed_ok or p != 2) else m.failed_marker(p))
        return "\n\n".join(out) + "\n"

    def test_failed_pages_are_recorded_kept_and_retried_alone(self):
        with tempfile.TemporaryDirectory() as d:
            pdf = make_pdf(d)
            env = Env(d, pages=3, md=lambda r: self.doc(r)).run([pdf, "-o", str(Path(d) / "o1")])
            self.assertIn("pages_failed: [2]", next((Path(d) / "o1").glob("*.md")).read_text())
            self.assertIn("<!-- pdf2md:failed page=2 -->", next((Path(d) / "o1").glob("*.md")).read_text())
            self.assertIn("note: 1 page(s) failed; run the same command again to retry only those pages", env.out.getvalue())
            r = result_of(env)
            self.assertEqual(r["pages_failed"], [2])
            self.assertTrue(r["cache_kept"])
            self.assertTrue(list((Path(d) / "cache").glob("*/slice_1_3.json")))       # kept without --keep-cache
            env2 = Env(d, pages=3, md=lambda r: self.doc(r, failed_ok=True)).run([pdf, "-o", str(Path(d) / "o2")])
            self.assertEqual([s[1] for s in env2.slices], [(2, 2)])                      # only the failed page re-read
            text = next((Path(d) / "o2").glob("*.md")).read_text()
            self.assertIn("P2 text", text)
            self.assertNotIn("pdf2md:failed", text)
            self.assertNotIn("pages_failed", text)
            self.assertEqual(result_of(env2)["pages_failed"], [])
            self.assertFalse(result_of(env2)["cache_kept"])
            self.assertEqual(list((Path(d) / "cache").glob("*")), [])

    def test_a_page_that_fails_again_stays_marked(self):
        with tempfile.TemporaryDirectory() as d:
            pdf = make_pdf(d)
            Env(d, pages=3, md=lambda r: self.doc(r)).run([pdf, "-o", str(Path(d) / "o1")])
            env2 = Env(d, pages=3, md=lambda r: self.doc(r)).run([pdf, "-o", str(Path(d) / "o2")])
            self.assertEqual(result_of(env2)["pages_failed"], [2])
            self.assertTrue(result_of(env2)["cache_kept"])

    def test_paddle_slice_marks_error_and_missing_pages(self):
        worker = textwrap.dedent("""
            import sys
            from pathlib import Path
            out = Path(sys.argv[1])
            for p in (1, 2, 4):
                if p == 2:
                    print('ERROR page 2: boom', flush=True); (out / 'page_0002').mkdir(); continue
                d = out / f'page_{p:04d}'; d.mkdir(); (d / 'x.md').write_text(f'text {p}\\n')
            sys.exit(1)
        """)
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "w.py"
            f.write_text(worker)
            cmd = lambda pdf, rng, server, out, scale=2.5: [sys.executable, str(f), str(out)]
            with mock.patch.object(m, "worker_command", cmd), contextlib.redirect_stderr(io.StringIO()) as err:
                md, st = m.paddle_slice(Path(d) / "b.pdf", (1, 5), Path(d) / "assets", "http://s/v1", False)
            self.assertEqual(notes.FAILED_MARKER.findall(md), ["2", "3", "5"])
            self.assertLess(md.index("text 1"), md.index("failed page=2"))
            self.assertLess(md.index("failed page=3"), md.index("text 4"))
            self.assertIn("exited with code 1", err.getvalue())

    def test_worker_failure_message_has_its_output(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "w.py"
            f.write_text("import sys\nprint('Traceback boom-line')\nsys.exit(7)\n")
            cmd = lambda pdf, rng, server, out, scale=2.5: [sys.executable, str(f)]
            with mock.patch.object(m, "worker_command", cmd), self.assertRaises(RuntimeError) as cm:
                m.paddle_slice(Path(d) / "b.pdf", (1, 2), Path(d) / "assets", "http://s/v1", False)
            self.assertIn("exit 7", str(cm.exception))
            self.assertIn("boom-line", str(cm.exception))

    def test_formula_image_pages_come_from_safety_json(self):
        worker = textwrap.dedent("""
            import json, sys
            from pathlib import Path
            out = Path(sys.argv[1])
            for p in (1, 2):
                d = out / f'page_{p:04d}'; d.mkdir(); (d / 'x.md').write_text('t\\n')
                rep = {k: [] for k in ('formula_retries', 'prose_restored', 'composite_figures')}
                rep['formula_images'] = [1] if p == 2 else []
                (d / 'safety.json').write_text(json.dumps([rep]))
        """)
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "w.py"
            f.write_text(worker)
            cmd = lambda pdf, rng, server, out, scale=2.5: [sys.executable, str(f), str(out)]
            with mock.patch.object(m, "worker_command", cmd):
                _, st = m.paddle_slice(Path(d) / "b.pdf", (1, 2), Path(d) / "assets", "http://s/v1", False)
            self.assertEqual(st["formula_image_pages"], [2])

    def test_worker_gets_utf8_and_only_appended_package_path(self):
        worker = ("import os, sys\nfrom pathlib import Path\n"
                  "d = Path(sys.argv[1]) / 'page_0001'; d.mkdir()\n"
                  "(d / 'x.md').write_text(os.environ['PYTHONIOENCODING'] + '|' + os.environ['PDF2MD_PACKAGE_PARENT'] + '|' + "
                  "os.environ.get('PYTHONPATH', 'none'))\n")
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PYTHONPATH", None)
            f = Path(d) / "w.py"
            f.write_text(worker)
            cmd = lambda pdf, rng, server, out, scale=2.5: [sys.executable, str(f), str(out)]
            with mock.patch.object(m, "worker_command", cmd):
                md, _ = m.paddle_slice(Path(d) / "b.pdf", (1, 1), Path(d) / "assets", "http://s/v1", False)
            self.assertIn(f"utf-8|{m.PACKAGE_PARENT}|none", md)


class ResultAndFrontmatterTests(unittest.TestCase):
    MD = ("<!-- pdf2md:page=1 -->\n\n# 1 One\n\ntext\n\n<!-- pdf2md:page=2 -->\n\n$$\nx^\n$$ broken\n\n"
          "<!-- pdf2md:page=3 -->\n\n# 2 Two\n\nb\n\n<!-- pdf2md:failed page=4 -->\n")

    def test_result_line_for_a_single_note(self):
        with tempfile.TemporaryDirectory() as d:
            env = Env(d, pages=3, md="<!-- pdf2md:page=1 -->\n\nhi\n", stats={"formula_image_pages": [1]}).run([make_pdf(d)])
            r = result_of(env)
            note = str((Path(d) / "paper.md").resolve())
            self.assertEqual(r, {"pdf": str(Path(d, "paper.pdf").resolve()), "output": note, "folder": None, "notes": [note],
                                 "pages_failed": [], "formula_images": [{"page": 1, "note": note}], "math_warnings": [],
                                 "cache_kept": False})
            self.assertTrue(env.out.getvalue().rstrip().endswith(env.out.getvalue().splitlines()[-1]))
            self.assertTrue(env.out.getvalue().splitlines()[-1].startswith("RESULT "))

    def test_result_line_for_a_split_book_maps_pages_to_notes(self):
        with tempfile.TemporaryDirectory() as d:
            env = Env(d, pages=4, md=self.MD, stats={"formula_image_pages": [2, 3]}).run(
                [make_pdf(d, "My Book.pdf"), "--split-chapters", "--pages", "1-4"])
            r = result_of(env)
            folder = (Path(d) / "My Book").resolve()
            self.assertEqual(r["folder"], str(folder))
            self.assertEqual(r["output"], str(folder / "00 Index.md"))
            self.assertEqual(r["notes"][0], str(folder / "00 Index.md"))
            self.assertEqual(r["pages_failed"], [4])
            self.assertTrue(r["cache_kept"])
            self.assertEqual(r["formula_images"], [{"page": 2, "note": str(folder / "01 One.md")},
                                                   {"page": 3, "note": str(folder / "02 Two.md")}])
            self.assertTrue(all(Path(p).is_absolute() and Path(p).exists() for p in r["notes"]))
            index = (folder / "00 Index.md").read_text()
            self.assertIn('pages: "1-4"\n', index)
            self.assertIn("pages_failed: [4]\n", index)
            self.assertNotIn("pdf2md:page", (folder / "01 One.md").read_text())
            self.assertFalse((folder / "00 Front matter.md").exists())           # only a page marker came before chapter 1
            for w in r["math_warnings"]:
                self.assertGreater(w["count"], 0)

    def test_pages_in_the_index_only_when_given(self):
        with tempfile.TemporaryDirectory() as d:
            Env(d, pages=10, md=BOOK.split("---\n", 1)[1]).run([make_pdf(d, "My Book.pdf"), "--split-chapters"])
            index = (Path(d) / "My Book" / "00 Index.md").read_text()
            self.assertNotIn("pages:", index)
            self.assertNotIn("pages_failed", index)


class YamlEscapingTests(unittest.TestCase):
    NASTY = 'He said "hi": back\\slash\nnew line   # [x] \'q\' é'

    def parse(self, text: str) -> dict:
        front = text.split("---\n")[1]
        try:
            import yaml
        except ImportError:
            return {l.split(": ", 1)[0]: json.loads(l.split(": ", 1)[1]) for l in front.splitlines()
                    if l.split(": ", 1)[1:2] and l.split(": ", 1)[1].startswith('"')}
        return yaml.safe_load(front)

    def test_values_round_trip_through_a_yaml_parser(self):
        note = {"title": self.NASTY, "kind": "chapter", "chapter": 1, "body": "x"}
        data = self.parse(notes.render_note(note, self.NASTY, self.NASTY, None, None, "00 Index", "2026-01-01", 0))
        self.assertEqual((data["title"], data["book"], data["source_pdf"]), (self.NASTY,) * 3)
        data = self.parse(notes.render_index([dict(note, file="01 A")], self.NASTY, self.NASTY, "2026-01-01"))
        self.assertEqual((data["title"], data["source_pdf"]), (self.NASTY,) * 2)

    def test_single_note_front_matter(self):
        with tempfile.TemporaryDirectory() as d:
            Env(d, pages=1).run([make_pdf(d, 'a "b": c\\d.pdf')])
            text = next(Path(d).glob("*.md")).read_text()
            data = self.parse(text)
            self.assertEqual(data["title"], 'a "b": c\\d')
            self.assertEqual(data["source_pdf"], 'a "b": c\\d.pdf')


class CacheDirTests(unittest.TestCase):
    def test_no_cache_temp_folder_is_removed_even_on_failure(self):
        made = []
        real = m.tempfile.mkdtemp
        for fail in (False, True):
            with tempfile.TemporaryDirectory() as d:
                def spy(*a, **k):
                    made.append(real(*a, **k))
                    return made[-1]
                with mock.patch.object(m.tempfile, "mkdtemp", spy):
                    try:
                        Env(d, pages=1, fail=["paper.pdf"] if fail else ()).run([make_pdf(d), "--no-cache"])
                    except SystemExit:
                        self.assertTrue(fail)
        self.assertEqual(len(made), 2)
        self.assertFalse(any(Path(p).exists() for p in made))

    def lock_path(self, d, pdf):
        return Path(d) / "cache" / m.run_key(Path(pdf), 120, 1, 100, False, 2.5, "obsidian") / "run.lock"

    def test_a_live_lock_blocks_a_second_run_and_a_stale_one_is_taken_over(self):
        with tempfile.TemporaryDirectory() as d:
            pdf = make_pdf(d)
            lock = self.lock_path(d, pdf)
            lock.parent.mkdir(parents=True)
            lock.write_text(str(os.getppid()))                       # the test runner's parent: alive, not us
            with self.assertRaises(SystemExit) as cm:
                Env(d).run([pdf])
            self.assertIn("another conversion of this PDF is running", str(cm.exception.code))
            self.assertFalse((Path(d) / "paper.md").exists())
            self.assertEqual(lock.read_text(), str(os.getppid()))      # not ours: left alone
            dead = subprocess.Popen([sys.executable, "-c", "pass"])
            dead.wait()
            lock.write_text(str(dead.pid))
            Env(d).run([pdf])
            self.assertTrue((Path(d) / "paper.md").exists())
            self.assertFalse(lock.exists())

    def test_lock_is_released_when_the_run_fails(self):
        with tempfile.TemporaryDirectory() as d:
            pdf = make_pdf(d)
            with self.assertRaises(SystemExit):
                Env(d, fail=["paper.pdf"]).run([pdf])
            self.assertFalse(self.lock_path(d, pdf).exists())

    def test_cache_put_uses_per_process_temp_names(self):
        with tempfile.TemporaryDirectory() as d:
            c = m.Cache(Path(d))
            seen = []
            real = Path.replace
            with mock.patch.object(Path, "replace", lambda self, t: (seen.append(self.name), real(self, t))[1]):
                c.put("slice_1_2", {"a": 1})
            self.assertEqual(seen, [f"slice_1_2.json.{os.getpid()}.tmp"])


class BackupOrderTests(unittest.TestCase):
    def test_backup_runs_before_figures_are_copied(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".obsidian").mkdir()
            env = Env(d, pages=1).run([make_pdf(d), "--backup-command", "BACKUP"])
            self.assertEqual(env.assets_at_backup, [False])
            self.assertTrue((Path(d) / "paper_assets" / "image_000000_abc.png").exists())


class VersionAndCancelTests(unittest.TestCase):
    def test_version_flags(self):
        for flag in ("--version", "-V"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                m.main([flag])
            self.assertEqual(buf.getvalue(), f"pdf2md-local {__version__}\n")
        r = subprocess.run([sys.executable, "-m", "pdf2md_local", "--version"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, f"pdf2md-local {__version__}\n"))

    @unittest.skipIf(os.name == "nt", "POSIX signals")
    def test_sigterm_exits_143_through_finally(self):
        ran = []
        old = signal.getsignal(signal.SIGTERM)

        def convert(a, pdf):
            try:
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(2)
            finally:
                ran.append(1)

        try:
            with tempfile.TemporaryDirectory() as d, mock.patch.object(m, "convert_pdf", convert):
                with self.assertRaises(SystemExit) as cm:
                    m.main([make_pdf(d), make_pdf(d, "b.pdf")])        # two PDFs: a cancel must not be swallowed
            self.assertEqual(cm.exception.code, 143)
            self.assertEqual(ran, [1])
        finally:
            signal.signal(signal.SIGTERM, old)

    @unittest.skipIf(os.name == "nt", "POSIX signals")
    def test_sigterm_stops_the_worker_and_the_server_it_started(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "server.py").write_text(textwrap.dedent("""
                import http.server, os, sys
                from pathlib import Path
                Path(sys.argv[2]).write_text(str(os.getpid()))
                class H(http.server.BaseHTTPRequestHandler):
                    def do_GET(self):
                        self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
                    def log_message(self, *a): pass
                http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()
            """))
            (d / "worker.py").write_text(textwrap.dedent("""
                import os, sys, time
                from pathlib import Path
                Path(os.environ['FAKE_WORKER_PID']).write_text(str(os.getpid()))
                time.sleep(120)
            """))
            import socket
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            pdf = make_pdf(d)
            home = d / "home"
            home.mkdir()
            env = {**os.environ, "HOME": str(home), "PDF2MD_HOME": str(home / "data"), "PDF2MD_TEST_WORKER_SCRIPT": str(d / "worker.py"),
                   "FAKE_WORKER_PID": str(d / "worker.pid"), "PYTHONPATH": str(ROOT)}
            serve = f"{sys.executable} {d / 'server.py'} {port} {d / 'server.pid'}"
            proc = subprocess.Popen([sys.executable, "-m", "pdf2md_local", pdf, "--pages", "1-3", "--no-backup",
                                     "--paddle-server", f"http://127.0.0.1:{port}/v1", "--serve-command", serve,
                                     "--serve-wait", "30", "-o", str(d / "out")],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, cwd=d)
            lines: queue.Queue = queue.Queue()
            threading.Thread(target=lambda: [lines.put(l) for l in proc.stdout], daemon=True).start()
            self.addCleanup(lambda: proc.poll() is None and proc.kill())
            seen = []
            deadline = time.time() + 30
            while time.time() < deadline and not any(l.startswith("PROGRESS") for l in seen):
                try:
                    seen.append(lines.get(timeout=0.5))
                except queue.Empty:
                    pass
            self.assertTrue(any(l.startswith("PROGRESS") for l in seen), seen)
            while time.time() < deadline and not (d / "worker.pid").exists():
                time.sleep(0.1)
            self.assertTrue((d / "worker.pid").exists(), "".join(seen))
            time.sleep(0.2)
            worker_pid, server_pid = int((d / "worker.pid").read_text()), int((d / "server.pid").read_text())
            self.assertTrue(alive(worker_pid) and alive(server_pid))
            start = time.time()
            proc.send_signal(signal.SIGTERM)
            try:
                rc = proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.fail("the CLI did not exit within 15 s of SIGTERM")
            self.assertEqual(rc, 143)
            self.assertLess(time.time() - start, 15)
            for _ in range(50):
                if not alive(worker_pid) and not alive(server_pid):
                    break
                time.sleep(0.1)
            self.assertFalse(alive(worker_pid), "worker still running")
            self.assertFalse(alive(server_pid), "server still running")
            self.assertFalse(list((home / ".cache" / "pdf2md").glob("*/run.lock")))


if __name__ == "__main__":
    unittest.main()
