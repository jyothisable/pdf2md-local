# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["marker-pdf==2.2.0"]
# ///
"""Marker 2 (used by the OCR-AI plugin). MODE=balanced|fast|no_ocr; balanced needs a Surya server (llama-server on PATH)."""
import os
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

from marker.converters.pdf import PdfConverter
from marker.models import create_model_dict
from marker.output import text_from_rendered

conv = PdfConverter(artifact_dict=create_model_dict(), config={"mode": os.environ.get("MODE", "balanced")})
run(lambda pdf: text_from_rendered(conv(pdf))[0])
