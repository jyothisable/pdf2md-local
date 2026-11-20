#!/usr/bin/env python3
"""Run pdf_to_md over an olmOCR-bench subset and write candidate folders for scoring.

Setup (once, outside the vault):
  uv venv --python 3.12 /tmp/olmbench/env
  uv pip install --python /tmp/olmbench/env/bin/python "olmocr[bench]" numpy
  /tmp/olmbench/env/bin/python -m playwright install chromium     # math tests render with KaTeX

Usage:
  python3 run_olmocr_bench.py fetch  /tmp/olmbench/data          # download a seeded subset
  python3 run_olmocr_bench.py run    /tmp/olmbench/data docling --no-llm
  python3 run_olmocr_bench.py run    /tmp/olmbench/data full
  /tmp/olmbench/env/bin/python -m olmocr.bench.benchmark --dir /tmp/olmbench/data

Each candidate is a folder inside <data> holding <category>/<pdf_stem>_pg1_repeat1.md files. The note's YAML
frontmatter is stripped before scoring so provenance metadata is not scored as page content.
"""
import json
import random
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = "https://huggingface.co/datasets/allenai/olmOCR-bench/resolve/main/bench_data/"
SUBSET = {"arxiv_math": 12, "table_tests": 6, "multi_column": 6, "headers_footers": 4}
TOOL = Path(__file__).resolve().parents[1] / "pdf_to_md.py"


def fetch(data: Path) -> None:
    random.seed(7)
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


def run(data: Path, name: str, flags: list[str]) -> None:
    cand = data / name
    pdfs = sorted((data / "pdfs").rglob("*.pdf"))
    for i, pdf in enumerate(pdfs, 1):
        rel = pdf.relative_to(data / "pdfs")          # keep the category folder: the scorer matches on it
        out = cand / rel.parent / f"{pdf.stem}_pg1_repeat1.md"
        if out.exists():
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        t = time.time()
        r = subprocess.run(["uv", "run", "--python", "3.12", str(TOOL), str(pdf), "-o", str(out),
                            "--no-backup", *flags], capture_output=True, text=True)
        if r.returncode != 0 or not out.exists():
            print(f"[{i}/{len(pdfs)}] FAILED {pdf.name}: {r.stderr[-300:]}", flush=True)
            out.write_text("")  # an empty page scores as failures instead of aborting the run
            continue
        out.write_text(re.sub(r"\A---\n.*?\n---\n\n?", "", out.read_text(), flags=re.S))
        print(f"[{i}/{len(pdfs)}] {pdf.name} {time.time() - t:.0f}s", flush=True)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "fetch":
        fetch(Path(sys.argv[2]))
    elif len(sys.argv) >= 4 and sys.argv[1] == "run":
        run(Path(sys.argv[2]), sys.argv[3], sys.argv[4:])
    else:
        sys.exit(__doc__)
