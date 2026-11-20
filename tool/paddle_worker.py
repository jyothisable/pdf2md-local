# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["paddlepaddle==3.2.1", "paddleocr[doc-parser]==3.7.0", "pypdfium2"]
# ///
"""PaddleOCR-VL-1.6 page worker. Run by pdf_to_md.py in its own environment; not meant to be called by hand.

PaddleOCR-VL = a layout model (PaddlePaddle, on the CPU) + a 0.9B vision-language model that reads each layout
block. The VLM is served by llama.cpp's `llama-server` (no Ollama, no PaddlePaddle GPU build needed).

  paddle_worker.py --pdf FILE --first 1 --last 40 --server http://127.0.0.1:8111/v1 --out DIR

Writes DIR/page_0007/<name>.md (+ imgs/) for every page, prints `PAGE n/N` as it goes, and `ERROR page` lines for
pages it could not read (a bad page never stops the rest).
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--first", type=int, default=1)
    ap.add_argument("--last", type=int, default=10**6)
    ap.add_argument("--server", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--scale", type=float, default=2.0, help="page render scale (2.0 = 144 dpi)")
    a = ap.parse_args()

    import pypdfium2
    from paddleocr import PaddleOCRVL

    pdf = pypdfium2.PdfDocument(a.pdf)
    last = min(a.last, len(pdf))
    pages = list(range(max(a.first, 1), last + 1))
    pipe = PaddleOCRVL(pipeline_version="v1.6", vl_rec_backend="llama-cpp-server", vl_rec_server_url=a.server,
                       device="cpu")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for n, page_no in enumerate(pages, 1):
            try:
                img = Path(tmp) / f"p{page_no}.png"
                pdf[page_no - 1].render(scale=a.scale).to_pil().convert("RGB").save(img)
                dst = out / f"page_{page_no:04d}"
                dst.mkdir(exist_ok=True)
                for res in pipe.predict(str(img)):
                    res.save_to_markdown(save_path=str(dst))
                print(f"PAGE {n}/{len(pages)}", flush=True)
            except Exception as e:  # noqa: BLE001 - one unreadable page must not lose the rest
                print(f"ERROR page {page_no}: {e.__class__.__name__}: {str(e)[:200]}", flush=True)
    pdf.close()


if __name__ == "__main__":
    sys.exit(main())
