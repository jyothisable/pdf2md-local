"""Backend setup helpers: safe extraction, resumable verified downloads, pinned llama.cpp. No network."""
import contextlib
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pdf2md_local import __version__, backend  # noqa: E402


def tar_with(path: Path, members):
    """members: (name, kind, data_or_target)."""
    with tarfile.open(path, "w:gz") as t:
        for name, kind, v in members:
            ti = tarfile.TarInfo(name)
            if kind == "file":
                ti.size = len(v)
                t.addfile(ti, io.BytesIO(v))
            else:
                ti.type = tarfile.SYMTYPE if kind == "sym" else tarfile.LNKTYPE
                ti.linkname = v
                t.addfile(ti)


class Resp(io.BytesIO):
    def __init__(self, body, status=200, **headers):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        for k, v in headers.items():
            self.headers[k.replace("_", "-")] = v

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, **headers):
    h = Message()
    for k, v in headers.items():
        h[k.replace("_", "-")] = v
    return urllib.error.HTTPError("http://x/f", code, "err", h, io.BytesIO())


class Server:
    """Fake urlopen: records requests, serves DATA with optional ETag/range support."""

    def __init__(self, data, etag='"e1"', ranges=True, truncate=None):
        self.data, self.etag, self.ranges, self.truncate, self.reqs = data, etag, ranges, truncate, []

    def __call__(self, req, timeout=None):
        self.reqs.append((req.get_method(), dict(req.header_items())))
        if req.get_method() == "HEAD":
            return Resp(b"", **({"ETag": self.etag} if self.etag else {}))
        rng = req.get_header("Range")
        if rng and self.ranges:
            start = int(rng.split("=")[1].rstrip("-"))
            if start >= len(self.data):
                raise http_error(416, Content_Range=f"bytes */{len(self.data)}")
            body = self.data[start:]
            return Resp(body, 206, Content_Length=str(len(body)),
                        Content_Range=f"bytes {start}-{len(self.data) - 1}/{len(self.data)}")
        body = self.data if self.truncate is None else self.data[:self.truncate]
        return Resp(body, 200, Content_Length=str(len(self.data)))


class ExtractTests(unittest.TestCase):
    def run_extract(self, members, fallback=False):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            arc, dest = d / "a.tar.gz", d / "out"
            dest.mkdir()
            tar_with(arc, members)
            with mock.patch.object(backend, "_has_tar_filter", return_value=not fallback):
                backend._safe_extract(arc, dest)
            return sorted(str(p.relative_to(dest)) for p in dest.rglob("*"))

    def test_normal(self):
        for fb in (False, True):
            got = self.run_extract([("b/llama-server", "file", b"x"), ("b/lib.so", "sym", "lib.so.1"),
                                    ("b/lib.so.1", "file", b"y")], fb)
            self.assertIn("b/llama-server", got)

    def test_symlink_escape_rejected(self):
        for fb in (False, True):
            with self.assertRaises(SystemExit) as c:
                self.run_extract([("lnk", "sym", "/tmp/x"), ("lnk/f", "file", b"x")], fb)
            self.assertIn("unsafe path", str(c.exception))

    def test_relative_symlink_escape_and_absolute_hardlink_rejected(self):
        for fb in (False, True):
            for m in ([("a/l", "sym", "../../etc")], [("h", "hard", "/etc/passwd")], [("../evil", "file", b"x")]):
                with self.assertRaises(SystemExit, msg=(fb, m)):
                    self.run_extract(m, fb)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name) / "f.bin"
        self.part = Path(self.tmp.name) / "f.bin.part"
        self.data = bytes(range(256)) * 40

    def dl(self, srv, size=None):
        with mock.patch("urllib.request.urlopen", srv):
            backend._download("http://x/f", self.dest, size)

    def test_fresh(self):
        self.dl(Server(self.data), len(self.data))
        self.assertEqual(self.dest.read_bytes(), self.data)
        self.assertFalse(self.part.exists())

    def test_resume_206_with_if_range(self):
        self.part.write_bytes(self.data[:1000])
        srv = Server(self.data)
        self.dl(srv, len(self.data))
        self.assertEqual(self.dest.read_bytes(), self.data)
        get = [h for m, h in srv.reqs if m == "GET"][0]
        self.assertEqual(get["Range"], "bytes=1000-")
        self.assertEqual(get["If-range"], '"e1"')

    def test_no_validator_restarts(self):
        self.part.write_bytes(b"junk" * 10)
        srv = Server(self.data, etag=None)
        self.dl(srv, len(self.data))
        self.assertEqual(self.dest.read_bytes(), self.data)
        self.assertNotIn("Range", [h for m, h in srv.reqs if m == "GET"][0])

    def test_200_restarts_from_zero(self):
        self.part.write_bytes(self.data[:1000])
        self.dl(Server(self.data, ranges=False), len(self.data))
        self.assertEqual(self.dest.read_bytes(), self.data)

    def test_416_on_complete_part_is_done(self):
        self.part.write_bytes(self.data)
        self.dl(Server(self.data))                    # expected size unknown: taken from the 416 header
        self.assertEqual(self.dest.read_bytes(), self.data)

    def test_truncated_rejected_and_resumable(self):
        with self.assertRaises(SystemExit) as c:
            self.dl(Server(self.data, truncate=500), len(self.data))
        self.assertIn("resume", str(c.exception))
        self.assertFalse(self.dest.exists())
        self.assertEqual(self.part.stat().st_size, 500)
        self.dl(Server(self.data), len(self.data))     # next run resumes
        self.assertEqual(self.dest.read_bytes(), self.data)

    def test_truncated_by_content_length_without_pin(self):
        with self.assertRaises(SystemExit):
            self.dl(Server(self.data, truncate=500))
        self.assertFalse(self.dest.exists())

    def test_network_error_is_friendly(self):
        def boom(req, timeout=None):
            raise urllib.error.URLError("down")
        with self.assertRaises(SystemExit) as c, mock.patch("urllib.request.urlopen", boom):
            backend._download("http://x/f", self.dest)
        self.assertIn("setup", str(c.exception))


class VerifyTests(unittest.TestCase):
    def test_mismatch_deletes(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "m"
            f.write_bytes(b"hello")
            backend._verify_file(f, 5, hashlib.sha256(b"hello").hexdigest(), "m")
            self.assertTrue(f.exists())
            with self.assertRaises(SystemExit) as c:
                backend._verify_file(f, 5, "0" * 64, "m")
            self.assertIn("checksum mismatch", str(c.exception))
            self.assertFalse(f.exists())

    def test_wrong_size_is_missing(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"MODEL_DIR": d}):
            for f, (size, _) in backend.MODEL_PINS.items():
                (Path(d) / f).write_bytes(b"short")
            self.assertFalse(backend.models_present())


class LlamaTests(unittest.TestCase):
    def release(self, digest):
        name = "llama-b12606-bin-ubuntu-x64.tar.gz"
        a = {"name": name, "size": 5, "browser_download_url": "http://x/a"}
        if digest:
            a["digest"] = digest
        return {"tag_name": "b12606", "assets": [a]}

    def install(self, rel, allow=False, data=b"hello"):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            return Resp(json.dumps(rel).encode())

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"PDF2MD_HOME": d}), \
                mock.patch("urllib.request.urlopen", fake_urlopen), \
                mock.patch.object(backend, "_driver_cuda", return_value=None) as cuda, \
                mock.patch("platform.system", return_value="Linux"), mock.patch("platform.machine", return_value="x86_64"), \
                mock.patch.object(backend, "_download", side_effect=lambda u, f, s=None: f.write_bytes(data)), \
                mock.patch.object(backend, "_safe_extract", side_effect=lambda f, d: (d / "llama-server").write_text("x")):
            backend.install_llama("cpu", allow)
        return calls, cuda

    def test_pinned_tag_and_cuda_once(self):
        calls, cuda = self.install(self.release("sha256:" + hashlib.sha256(b"hello").hexdigest()))
        self.assertTrue(calls[0].endswith("/releases/tags/" + backend.LLAMA_CPP_TAG))
        self.assertEqual(cuda.call_count, 1)

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"PDF2MD_LLAMA_TAG": "b1"}):
            calls, _ = self.install(self.release("sha256:" + hashlib.sha256(b"hello").hexdigest()))
        self.assertTrue(calls[0].endswith("/tags/b1"))

    def test_missing_digest_fails_unless_allowed(self):
        with self.assertRaises(SystemExit) as c:
            self.install(self.release(None))
        self.assertIn("--allow-unverified", str(c.exception))
        self.install(self.release(None), allow=True)

    def test_digest_mismatch_exits(self):
        with self.assertRaises(SystemExit) as c:
            self.install(self.release("sha256:" + "0" * 64))
        self.assertIn("checksum mismatch", str(c.exception))


class CheckJsonTests(unittest.TestCase):
    def test_version_key(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"PDF2MD_HOME": d, "MODEL_DIR": d}), \
                mock.patch.object(backend, "worker_imports_ok", return_value=False), \
                mock.patch.object(backend, "server_reachable", return_value=False), \
                mock.patch.object(backend, "gpu_name", return_value=None), \
                contextlib.redirect_stdout(out):
            backend.main(["check", "--json"])
        data = json.loads(out.getvalue())
        self.assertEqual(data["version"], __version__)
        self.assertIn("checks", data)
        self.assertIn("ready", data)


if __name__ == "__main__":
    unittest.main()
