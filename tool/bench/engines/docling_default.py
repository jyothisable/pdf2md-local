# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["docling==2.150.1"]
# ///
"""Docling with its default pipeline (what plugins that call the `docling` CLI get)."""
import json, sys, time
from pathlib import Path

ITEMS = json.load(open(sys.argv[1]))   # [{"pdf": ..., "out": ...}]


def run(convert):
    """Convert every item with convert(pdf_path) -> markdown; one failure never stops the run."""
    for i, it in enumerate(ITEMS, 1):
        t = time.time()
        out = Path(it["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = convert(it["pdf"])
        except Exception as e:  # noqa: BLE001
            text = ""
            print(f"  FAILED {Path(it['pdf']).name}: {e.__class__.__name__}: {str(e)[:160]}", flush=True)
        out.write_text(text or "", encoding="utf-8")
        print(f"[{i}/{len(ITEMS)}] {Path(it['pdf']).name} {time.time() - t:.1f}s", flush=True)

from docling.document_converter import DocumentConverter

conv = DocumentConverter()
run(lambda pdf: conv.convert(pdf).document.export_to_markdown())
