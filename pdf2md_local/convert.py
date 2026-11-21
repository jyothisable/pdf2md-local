"""PDF -> Markdown with PaddleOCR-VL-1.6, locally.

Paddle's layout model reads each page and a 0.9B vision-language model, served by llama.cpp's llama-server, reads
each block. The result is finished here:
  - maths is normalised: inline $...$, display $$ on their own lines; the LaTeX is checked for rendering errors
  - source-backed guards retry or replace damaged equations/prose with the PDF's own text or an exact source crop
  - headings are repaired from the PDF's bookmarks, optional links to the PDF page are added under them, and books
    can be split into chapter notes with contents and Previous/Next links
  - the note is written to a NEW file (never overwrites); --flavor picks plain Markdown or Obsidian syntax
  - long runs checkpoint per slice and resume; one bad page is retried, then marked, never fatal

Run:  pdf2md setup           (once: worker environment + model download)
      pdf2md serve           (model server, or let --serve-command "pdf2md serve" start it)
      pdf2md check           (is everything ready?)
      pdf2md paper.pdf [more.pdf ...]            (see pdf2md --help)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request  # noqa: F401 (tests reach m.urllib)
from pathlib import Path

FLAVORS = ("auto", "plain", "obsidian")
PADDLE_PAGES_PER_BATCH = 120   # the Paddle worker loads its models once per slice, so use bigger slices
DEFAULT_PADDLE_SERVER = "http://127.0.0.1:8111/v1"
CACHE_ROOT = Path.home() / ".cache" / "pdf2md"

from pdf2md_local.latex import CHUNK_CHARS, NOT_DECODED  # noqa: F401
from pdf2md_local.latex import (  # noqa: F401
    BRACKET_BLOCK, DOLLAR_BLOCK, DOLLAR_INLINE, FENCE_RE, LATEX_TOKEN, PAREN_INLINE, SINGLE_LETTERS, SUBSCRIPTS,
    SUPERSCRIPTS, TEXT_CMD, TEXT_MACROS, TRAILING_TAG, blank_text_groups, latex_problems, normalize_math,
    simplify_inline_math, simplify_light_math, split_chunks, text_macros_to_unicode, tidy_block, tidy_tex,
    validate_document,
)
from pdf2md_local.guards import (  # noqa: F401
    composite_figure_groups, formula_body, formula_delimiter_problems, formula_retry_problems,
    inline_math_problems, native_greek_problems, native_matrix_problems, native_operator_problems,
    native_variable_problems, protect_paddle_result, source_verified_prose,
)
from pdf2md_local.outline import (  # noqa: F401
    CAPTION_START, CHAPTER_PAIR, CODE_HEADING, HEADING, NUMBERED_HEADING, TRAILING_PAGE, fix_headings, heading_key,
    link_target, normalize_pdf_outline, not_a_heading, read_pdf_outline, repair_pdf_headings, wants_page_link,
)
from pdf2md_local.notes import (  # noqa: F401
    AMP_ENTITY, BACK_HEADING, CHAPTER_H1, PAGE_MARKER, UNSAFE_NAME, clean_paddle_markdown, find_chapters,
    FAILED_MARKER, failed_marker, html_table_to_md, note_index_for_page, render_index, render_note, safe_name, split_book,
    split_book_paged, strip_page_markers, table_cell_breaks, yaml_str,
    to_plain, unescape_entities, unique_dir, unique_path,
)
from pdf2md_local.runner import (  # noqa: F401
    Cache, CacheLock, PACKAGE_PARENT, ServerGuard, WORKER, api_base, convert_resilient, merge_stats, new_stats, openai_chat,
    paddle_slice, page_count, page_ranges, parse_pages, run_key, worker_command,
)



def find_vault_root(p: Path) -> Path | None:
    """The nearest ancestor folder that holds an `.obsidian` directory, if any."""
    for d in [p.resolve(), *p.resolve().parents]:
        if (d / ".obsidian").is_dir():
            return d
    return None


def run_backup(command: str) -> None:
    """Run the user's backup command; a failure aborts before anything is written."""
    r = subprocess.run(shlex.split(command), capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"error: backup command failed (exit {r.returncode}), nothing written:\n{r.stdout}{r.stderr}")
    shown = ""
    try:
        shown = json.loads(r.stdout).get("archive", "")
    except (ValueError, AttributeError):
        shown = next((l for l in reversed(r.stdout.splitlines()) if l.strip()), "")
    print(f"backup: {shown or 'done'}")


def reread_failed(md: str, st: dict, failed: list[int], convert_fn) -> tuple[str, dict, list[int]]:
    """Re-read only the failed pages of a cached slice and splice the results over their markers."""
    for p in sorted(set(failed)):
        marker = failed_marker(p)
        if marker not in md:
            continue
        still: list[int] = []
        new, st_p = convert_resilient(convert_fn, (p, p), still, None)
        md = md.replace(marker, new.strip("\n"), 1)
        st = merge_stats(st, st_p)
    return md, st, sorted(set(int(x) for x in FAILED_MARKER.findall(md)))


def convert_pdf(a: argparse.Namespace, pdf: Path) -> None:
    """Convert one PDF according to the parsed arguments."""
    batch_pages = a.pages_per_batch if a.pages_per_batch is not None else PADDLE_PAGES_PER_BATCH
    flavor = a.flavor if a.flavor != "auto" else ("obsidian" if find_vault_root(a.out.expanduser().resolve() if a.out else pdf.parent) else "plain")
    flavor_key = "obsidian"          # checkpoints always hold Obsidian-style text; the flavour is applied when writing
    page_links = a.page_links == "on" or (a.page_links == "auto" and flavor == "obsidian")
    engines = ["paddleocr-vl-1.6"]

    out = (a.out.expanduser().resolve() if a.out else pdf.parent)
    if out.suffix.lower() != ".md":
        out = out / f"{pdf.stem}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out = unique_path(out)
    assets = out.parent / f"{out.stem}_assets"

    total = page_count(pdf)
    first, last = parse_pages(a.pages, total)
    key = run_key(pdf, batch_pages, first, last, page_links, a.paddle_scale, flavor_key)
    cache_dir = Path(tempfile.mkdtemp(prefix="pdf2md-")) if a.no_cache else CACHE_ROOT / key
    lock = None
    try:
        cache = Cache(cache_dir)
        if not a.no_cache:
            lock = CacheLock(cache_dir)
            lock.acquire()
        _convert_locked(a, pdf, cache, cache_dir, out, assets, total, first, last, batch_pages, flavor, page_links, engines)
    finally:
        if a.no_cache:
            shutil.rmtree(cache_dir, ignore_errors=True)
        if lock:
            lock.release()


def _convert_locked(a, pdf, cache, cache_dir, out, assets, total, first, last, batch_pages, flavor, page_links, engines):
    t0 = time.time()
    ranges = page_ranges(last, batch_pages, first)
    resumed = len(list(cache_dir.glob("slice_*.json")))
    print(f"[1/4] PaddleOCR-VL: {pdf.name} ({(last - first + 1) if last else '?'} of {total or '?'} pages, {len(ranges)} slice(s)"
          + ")" + (f"; resuming, {resumed} slice(s) already done" if resumed else ""))

    totals = new_stats()
    failed_pages: set[int] = set()
    pieces: list[str] = []
    total_pages = (last - first + 1) if last else 0

    def progress(done: int) -> None:
        """A machine-readable line for the plugin's progress window: pages finished of pages to convert."""
        if total_pages:
            print(f"PROGRESS {max(0, min(done, total_pages))} {total_pages}", flush=True)

    from pdf2md_local import backend
    if not os.environ.get("PDF2MD_TEST_WORKER_SCRIPT"):      # (tests replace the worker)
        backend.require_ready()
    guard = ServerGuard(a.paddle_server, a.serve_command, a.serve_wait)
    try:
        guard.__enter__()
        progress(0)
        for n, rng in enumerate(ranges, 1):
            ck = f"slice_{rng[0]}_{rng[1]}"
            hit = cache.get(ck)
            convert_fn = lambda r: paddle_slice(pdf, r, cache.img, a.paddle_server, page_links,
                                                lambda i, _t, r=r: progress(r[0] - first + i), a.paddle_scale)
            if hit:
                md, st = hit["md"], hit["stats"]
                failed_here = sorted(set(hit["failed"]) | {int(x) for x in FAILED_MARKER.findall(md)})
                if failed_here:
                    quiet = lambda r: paddle_slice(pdf, r, cache.img, a.paddle_server, page_links, None, a.paddle_scale)
                    print(f"  re-reading {len(failed_here)} failed page(s): {failed_here}", flush=True)
                    md, st, failed_here = reread_failed(md, st, failed_here, quiet)
                    cache.put(ck, {"md": md, "stats": st, "failed": failed_here})
            else:
                failed_here = []
                md, st = convert_resilient(convert_fn, rng, failed_here, None)
                failed_here = sorted(set(failed_here) | {int(x) for x in FAILED_MARKER.findall(md)})
                cache.put(ck, {"md": md, "stats": st, "failed": failed_here})
            failed_pages |= set(failed_here)
            totals = merge_stats(totals, st)
            progress(rng[1] - first + 1)
            md = unescape_entities(normalize_math(md))
            el = time.time() - t0
            eta = f", ETA ~{el / n * (len(ranges) - n) / 60:.0f} min" if len(ranges) > 1 and n < len(ranges) else ""
            label = f"slice {n}/{len(ranges)}" + (f" (pages {rng[0]}-{rng[1]})" if len(ranges) > 1 else "")
            print(f"  {label}: {st['eq_total']} equation(s) [{el:.0f}s{eta}]", flush=True)
            pieces += split_chunks(md)

        print(f"      equations: {totals['eq_total']} (re-read {totals['eq_repaired']}, "
              f"kept as source picture {totals['eq_pictured']})")
        print(f"      source guards: {totals['prose_restored']} prose block(s) restored, "
              f"{totals['composite_figures']} composite figure(s) preserved, "
              f"{totals['inline_source_restored']} inline paragraph(s) restored, "
              f"{totals['inline_source_images']} inline paragraph(s) kept as source images, "
              f"{totals['running_headers']} repeated running header(s) removed")
        md = normalize_math("\n\n".join(d.strip("\n") for d in pieces))
    finally:
        guard.__exit__()

    md = fix_headings(md)
    try:
        outline = read_pdf_outline(pdf)
        md, heading_report = repair_pdf_headings(md, outline)
        if outline:
            print(f"      PDF outline: {heading_report['matched']}/{len(outline)} headings matched; "
                  f"{len(heading_report['changes'])} repaired, {len(heading_report['ambiguous'])} ambiguous, "
                  f"{len(heading_report['unmatched'])} unmatched (left unchanged)")
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        print(f"warning: PDF outline unavailable; using extracted headings ({type(exc).__name__})")
    failed_pages |= {int(x) for x in FAILED_MARKER.findall(md)}
    failed = sorted(failed_pages)
    if failed:
        print(f"warning: {len(failed)} page(s) could not be converted: {failed}")
    if not resumed and total_pages:
        try:
            backend.record_rate(total_pages, time.time() - t0)
        except Exception:  # noqa: BLE001  (a speed estimate must never fail a conversion)
            pass
    formula_pages = sorted(set(totals.get("formula_image_pages", [])))
    marked = md                                  # page markers still in; needed to find which note holds a page
    md = md if a.page_markers else strip_page_markers(md)
    issues = validate_document(md)
    print(f"[3/4] math check: {len(issues)} problem(s)")
    for ln, why, snip in issues[:20]:
        print(f"  body line {ln}: {why}: {snip}")
    if len(issues) > 20:
        print(f"  ... {len(issues) - 20} more")

    date = dt.date.today().isoformat()
    notes = split_book_paged(marked, pdf.stem, a.page_markers) if a.split_chapters else None
    if a.split_chapters and not notes:
        print("warning: no numbered chapter headings found; writing a single note instead")
    folder = unique_dir(out.with_suffix("")) if notes else None
    if folder:
        assets = folder / "assets"

    # Back up first: nothing (not even a figure) is written into the vault before this has succeeded.
    if a.backup_command and not a.no_backup and find_vault_root(out.parent):
        run_backup(a.backup_command)
    if any(cache.img.iterdir()):  # figures: copy missing files, never overwrite
        assets.mkdir(parents=True, exist_ok=True)
        for f in cache.img.iterdir():
            if not (assets / f.name).exists():
                shutil.copy2(f, assets / f.name)

    written: list[Path] = []
    math_warnings: list[dict] = []
    formula_images: list[dict] = []
    if notes:
        folder.mkdir(parents=True, exist_ok=True)
        index_stem = "00 Index"
        files = [n["file"] for n in notes]
        for i, n in enumerate(notes):
            count = len(validate_document(n["body"]))
            text = render_note(n, pdf.stem, pdf.name, files[i - 1] if i else None,
                               files[i + 1] if i + 1 < len(files) else None, index_stem, date, count)
            if flavor == "plain":
                text = to_plain(text, "assets", os.path.relpath(pdf, folder))
            path = folder / f"{n['file']}.md"
            with open(path, "x", encoding="utf-8") as f:
                f.write(text)
            n["path"] = path
            if count:
                math_warnings.append({"note": str(path), "count": count})
        index_path = folder / f"{index_stem}.md"
        with open(index_path, "x", encoding="utf-8") as f:
            index_text = render_index(notes, pdf.stem, pdf.name, date, failed, f"{first}-{last}" if a.pages else None)
            f.write(to_plain(index_text, "assets", os.path.relpath(pdf, folder)) if flavor == "plain" else index_text)
        written = [index_path] + [n["path"] for n in notes]
        formula_images = [{"page": p, "note": str(notes[note_index_for_page(notes, p)]["path"])} for p in formula_pages]
        main_note = index_path
        print(f"[4/4] wrote {len(notes)} notes + index to {folder} ({time.time() - t0:.0f}s)")
        for n in notes:
            print(f"      {n['file']}  ({n['body'].count(chr(10)) + 1} lines)")
    else:
        folder = None
        front = (f"---\ntitle: {yaml_str(pdf.stem)}\nsource_pdf: {yaml_str(pdf.name)}\n"
                 f"converted: {date}\n"
                 f"converter: {' + '.join(engines)}\n"
                 f"math_warnings: {len(issues)}\n"
                 + (f"pages: \"{first}-{last}\"\n" if a.pages else "")
                 + (f"pages_failed: {failed}\n" if failed else "")
                 + "tags: [pdf_import]\n---\n\n")
        with open(out, "x", encoding="utf-8") as f:  # exclusive: never overwrite
            body = front + md.strip() + "\n"
            f.write(to_plain(body, assets.name, os.path.relpath(pdf, out.parent)) if flavor == "plain" else body)
        written = [out]
        main_note = out
        if issues:
            math_warnings.append({"note": str(out), "count": len(issues)})
        formula_images = [{"page": p, "note": str(out)} for p in formula_pages]
        print(f"[4/4] wrote {out} ({time.time() - t0:.0f}s)")
    if assets.exists():
        print(f"      figures: {assets}")
    keep = bool(a.keep_cache and not a.no_cache) or bool(failed)
    if failed:
        print(f"note: {len(failed)} page(s) failed; run the same command again to retry only those pages", flush=True)
    if not keep:
        shutil.rmtree(cache_dir, ignore_errors=True)
    print("RESULT " + json.dumps({
        "pdf": str(pdf), "output": str(main_note), "folder": str(folder) if folder else None,
        "notes": [str(p) for p in written], "pages_failed": failed, "formula_images": formula_images,
        "math_warnings": math_warnings, "cache_kept": keep and not a.no_cache,
    }, ensure_ascii=True), flush=True)


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("--version", "-V"):
        from pdf2md_local import __version__
        print(f"pdf2md-local {__version__}")
        return
    try:       # a cancel (SIGTERM) behaves like Ctrl-C: `finally` blocks stop the worker and any server we started
        signal.signal(signal.SIGTERM, lambda _s, _f: sys.exit(143))
    except (ValueError, OSError):      # not the main thread
        pass
    if argv and argv[0] in ("setup", "check", "serve", "selftest", "info"):
        from pdf2md_local import backend
        sys.exit(backend.main(argv))
    ap = argparse.ArgumentParser(
        prog="pdf2md", description="Convert PDFs to Markdown locally (maths as LaTeX, figures, tables, chapters). "
        "First run `pdf2md setup` once; `pdf2md check` verifies it; `pdf2md serve` starts the model server.")
    ap.add_argument("pdf", type=Path, nargs="+", help="one or more PDFs, converted one after another")
    ap.add_argument("-o", "--out", type=Path, help="output .md path or directory (default: next to the PDF)")
    ap.add_argument("--flavor", choices=FLAVORS, default="auto",
                    help="plain = standard Markdown links and images; obsidian = wikilinks and ![[embeds]]; "
                         "auto = obsidian when the output is inside an Obsidian vault (default)")
    ap.add_argument("--page-links", choices=("auto", "on", "off"), default="auto",
                    help="add a link to the PDF page under each heading; auto = in the obsidian flavour")
    ap.add_argument("--paddle-server", default=os.environ.get("PDF2MD_PADDLE_SERVER", DEFAULT_PADDLE_SERVER),
                    help="address of the llama-server running PaddleOCR-VL (default: $PDF2MD_PADDLE_SERVER, "
                         f"{DEFAULT_PADDLE_SERVER})")
    ap.add_argument("--paddle-scale", type=float, default=float(os.environ.get("PDF2MD_PADDLE_SCALE", "2.5")),
                    help="page render scale: 2.5 (180 dpi, balanced default), 4 (288 dpi, table-heavy PDFs)")
    ap.add_argument("--serve-command", default=os.environ.get("PDF2MD_SERVE_COMMAND"),
                    help="command that starts that server; run only if nothing answers, stopped when done "
                         "(default: $PDF2MD_SERVE_COMMAND; e.g. pdf2md serve)")
    ap.add_argument("--serve-wait", type=int, default=180, help="seconds to wait for --serve-command's server")
    ap.add_argument("--pages-per-batch", type=int, default=None,
                    help=f"convert big PDFs in slices of N pages (default {PADDLE_PAGES_PER_BATCH}; 0 = whole file)")
    ap.add_argument("--pages", help="convert only these pages, e.g. 10-40 (1-based, inclusive)")
    ap.add_argument("--split-chapters", action="store_true",
                    help="write a folder of notes (contents, front matter, one note per chapter, back matter) "
                         "instead of one big note; needs numbered chapter headings")
    ap.add_argument("--backup-command", default=os.environ.get("PDF2MD_BACKUP_COMMAND"),
                    help="command to run before writing into an Obsidian vault (default: $PDF2MD_BACKUP_COMMAND, none)")
    ap.add_argument("--page-markers", action="store_true",
                    help="keep the hidden <!-- pdf2md:page=N --> comment that starts every page (removed by default)")
    ap.add_argument("--no-backup", action="store_true", help="skip the backup command")
    ap.add_argument("--no-cache", action="store_true", help="no checkpointing (an interrupted run cannot resume)")
    ap.add_argument("--keep-cache", action="store_true", help="keep the checkpoint folder after success")
    a = ap.parse_args(argv)
    if not 1 <= a.paddle_scale <= 4:
        ap.error("--paddle-scale must be between 1 and 4")

    pdfs = [p.expanduser().resolve() for p in a.pdf]
    bad = [str(p) for p in pdfs if not p.is_file() or p.suffix.lower() != ".pdf"]
    if bad:
        sys.exit("error: not a PDF file: " + ", ".join(bad))
    if len(pdfs) > 1 and a.out and a.out.suffix.lower() == ".md":
        sys.exit("error: with several PDFs, -o must be a folder, not a .md file")
    failures: list[str] = []
    for i, pdf in enumerate(pdfs, 1):
        if len(pdfs) > 1:
            print(f"\n=== PDF {i}/{len(pdfs)}: {pdf.name}", flush=True)
        try:
            convert_pdf(a, pdf)
        except SystemExit as e:       # one bad PDF must not stop the rest of a batch
            if len(pdfs) == 1 or e.code == 143:
                raise
            print(f"error: {pdf.name}: {e.code}", file=sys.stderr, flush=True)
            failures.append(pdf.name)
    if failures:
        sys.exit(f"error: {len(failures)} of {len(pdfs)} PDF(s) failed: " + "; ".join(failures))


if __name__ == "__main__":
    main()
