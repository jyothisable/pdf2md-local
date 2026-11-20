#!/usr/bin/env python3
"""Run pdf_to_md, or another PDF-to-Markdown engine, over an olmOCR-bench subset, then score and compare.

Setup (once, outside any vault):
  uv venv --python 3.12 /tmp/olmbench/env
  uv pip install --python /tmp/olmbench/env/bin/python "olmocr[bench]" numpy
  /tmp/olmbench/env/bin/python -m playwright install chromium     # math tests render with KaTeX

Usage (D = the data folder, e.g. /tmp/olmbench/data):
  run_olmocr_bench.py fetch   D                          # download a seeded subset: 8 categories, 46 PDFs
  run_olmocr_bench.py run     D NAME [pdf2md flags...]   # convert with pdf_to_md.py
  run_olmocr_bench.py engine  D NAME ENGINE [KEY=VAL..]  # convert with engines/ENGINE.py (docling_default, marker_run ...)
  run_olmocr_bench.py score   D NAME                     # official scorer -> D/../NAME.summary.txt and NAME.fails.txt
  run_olmocr_bench.py compare D NAME [NAME ...]          # table: per-category score and time, side by side

A candidate is a folder inside D holding <category>/<pdf_stem>_pg1_repeat1.md files. For pdf_to_md output the
YAML frontmatter is stripped before scoring so provenance metadata is not scored as page content. Timings are
written to D/NAME.times.json.
"""
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = "https://huggingface.co/datasets/allenai/olmOCR-bench/resolve/main/bench_data/"
SUBSET = {"arxiv_math": 12, "table_tests": 6, "multi_column": 6, "headers_footers": 4,   # the first four: as before
          "old_scans": 6, "old_scans_math": 6, "long_tiny_text": 6}                      # added for all 8 categories
HERE = Path(__file__).resolve().parent
TOOL = HERE.parent / "pdf_to_md.py"
PY = os.environ.get("OLMBENCH_PYTHON", "/tmp/olmbench/env/bin/python")


def fetch(data: Path) -> None:
    random.seed(7)  # the same seed and order as before, so the first four categories keep their PDFs
    for cat, n in SUBSET.items():
        rows = [json.loads(l) for l in urllib.request.urlopen(BASE + cat + ".jsonl") if l.strip()]
        pick = set(random.sample(sorted({r["pdf"] for r in rows}), n))
        data.mkdir(parents=True, exist_ok=True)
        with open(data / f"{cat}.jsonl", "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows if r["pdf"] in pick)
        for p in pick:
            dst = data / "pdfs" / p
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                urllib.request.urlretrieve(BASE + "pdfs/" + p, dst)
        print(cat, len(pick), "pdfs")


def pdfs_of(data: Path) -> list[Path]:
    return sorted((data / "pdfs").rglob("*.pdf"))


def out_path(data: Path, name: str, pdf: Path) -> Path:
    rel = pdf.relative_to(data / "pdfs")          # keep the category folder: the scorer matches on it
    return data / name / rel.parent / f"{pdf.stem}_pg1_repeat1.md"


def save_times(data: Path, name: str, times: dict) -> None:
    f = data / f"{name}.times.json"
    old = json.loads(f.read_text()) if f.exists() else {}
    f.write_text(json.dumps({**old, **times}, indent=1))


def run(data: Path, name: str, flags: list[str]) -> None:
    times = {}
    pdfs = pdfs_of(data)
    for i, pdf in enumerate(pdfs, 1):
        out = out_path(data, name, pdf)
        if out.exists():
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        t = time.time()
        r = subprocess.run(["uv", "run", "--python", "3.12", str(TOOL), str(pdf), "-o", str(out), "--no-backup", *flags],
                           capture_output=True, text=True)
        if r.returncode != 0 or not out.exists():
            print(f"[{i}/{len(pdfs)}] FAILED {pdf.name}: {r.stderr[-300:]}", flush=True)
            out.write_text("")  # an empty page scores as failures instead of aborting the run
            continue
        out.write_text(re.sub(r"\A---\n.*?\n---\n\n?", "", out.read_text(), flags=re.S))
        times[pdf.stem] = time.time() - t
        print(f"[{i}/{len(pdfs)}] {pdf.name} {times[pdf.stem]:.0f}s", flush=True)
    save_times(data, name, times)


def engine(data: Path, name: str, engine_name: str, env_pairs: list[str]) -> None:
    """One process per engine, so models load once. Output lines '[i/N] file Xs' become the timings."""
    script = HERE / "engines" / f"{engine_name}.py"
    items = []
    for pdf in pdfs_of(data):
        out = out_path(data, name, pdf)
        if not out.exists():
            items.append({"pdf": str(pdf), "out": str(out)})
    if not items:
        print("nothing to do")
        return
    manifest = data / f"{name}.manifest.json"
    manifest.write_text(json.dumps(items))
    env = {**os.environ, **dict(p.split("=", 1) for p in env_pairs)}
    t0 = time.time()
    proc = subprocess.Popen(["uv", "run", "--python", "3.12", str(script), str(manifest)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    times = {}
    for line in proc.stdout:
        print(line.rstrip(), flush=True)
        m = re.match(r"\[\d+/\d+\] (.+?)\.pdf ([\d.]+)s", line)
        if m:
            times[m.group(1)] = float(m.group(2))
    proc.wait()
    times["__total__"] = time.time() - t0
    save_times(data, name, times)
    print(f"{engine_name}: exit {proc.returncode}, {len(items)} pdfs, {times['__total__']:.0f}s total")


def score(data: Path, name: str, boot: int = 100) -> None:
    res = data.parent
    r = subprocess.run([PY, "-m", "olmocr.bench.benchmark", "--dir", str(data), "--candidate", name,
                        "--bootstrap_samples", str(boot)], capture_output=True, text=True)
    text = r.stdout + r.stderr
    (res / f"{name}.score.txt").write_text(text)
    body = "\n".join(l for l in text.splitlines() if "Cleaning up" not in l)
    (res / f"{name}.summary.txt").write_text(body[body.find("Final Summary"):] if "Final Summary" in body else body[-2000:])
    (res / f"{name}.fails.txt").write_text("\n".join(l for l in text.splitlines() if "[FAIL]" in l))


CATS = ["arxiv_math", "old_scans_math", "table_tests", "old_scans", "headers_footers", "multi_column", "long_tiny_text", "baseline"]


def parse_summary(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for l in path.read_text().splitlines():
        m = re.match(r"\s*(\w+)(?:\.jsonl)?\s*:\s*([\d.]+)% \((\d+)/(\d+) tests\)", l)
        if m:
            out[m.group(1)] = (float(m.group(2)), int(m.group(4)))
        m = re.search(r"Average Score: ([\d.]+)% ± ([\d.]+)%", l)
        if m:
            out["__overall__"] = (float(m.group(1)), float(m.group(2)))
    return out


def compare(data: Path, names: list[str]) -> None:
    short = {"arxiv_math": "arXiv math", "old_scans_math": "scans math", "table_tests": "tables", "old_scans": "old scans",
             "headers_footers": "hdr/ftr", "multi_column": "multi-col", "long_tiny_text": "tiny text", "baseline": "base"}
    rows = {n: parse_summary(data.parent / f"{n}.summary.txt") for n in names}
    head = ["engine"] + [short[c] for c in CATS] + ["OVERALL", "s/PDF"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for n in names:
        r = rows[n]
        tf = data / f"{n}.times.json"
        t = json.loads(tf.read_text()) if tf.exists() else {}
        per = [v for k, v in t.items() if not k.startswith("__")]
        secs = (t["__total__"] / max(len(per), 1)) if "__total__" in t else (sum(per) / len(per) if per else float("nan"))
        cells = [f"{r[c][0]:.1f}" if c in r else "-" for c in CATS]
        ov = r.get("__overall__")
        lines.append("| " + " | ".join([n] + cells + [f"**{ov[0]:.1f}** ±{ov[1]:.1f}" if ov else "-", f"{secs:.0f}"]) + " |")
    n_tests = {c: next((rows[n][c][1] for n in names if c in rows[n]), 0) for c in CATS}
    lines.append("| (tests per category) | " + " | ".join(str(n_tests[c]) for c in CATS) + " | | |")
    print("\n".join(lines))


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) >= 2 and a[0] == "fetch":
        fetch(Path(a[1]))
    elif len(a) >= 3 and a[0] == "run":
        run(Path(a[1]), a[2], a[3:])
    elif len(a) >= 4 and a[0] == "engine":
        engine(Path(a[1]), a[2], a[3], a[4:])
    elif len(a) >= 3 and a[0] == "score":
        score(Path(a[1]), a[2])
    elif len(a) >= 3 and a[0] == "compare":
        compare(Path(a[1]), a[2:])
    else:
        sys.exit(__doc__)
