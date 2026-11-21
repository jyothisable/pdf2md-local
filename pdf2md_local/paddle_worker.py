"""PaddleOCR-VL-1.6 page worker. Run by pdf2md in the environment `pdf2md setup` creates; not meant to be called by hand.

PaddleOCR-VL = a layout model (PaddlePaddle, on the CPU) + a 0.9B vision-language model that reads each layout
block. The VLM is served by llama.cpp's `llama-server` (no PaddlePaddle GPU build needed).

  paddle_worker.py --pdf FILE --first 1 --last 40 --server http://127.0.0.1:8111/v1 --out DIR

Writes DIR/page_0007/<name>.md (+ imgs/) for every page, prints `PAGE n/N` as it goes, and `ERROR page` lines for
pages it could not read (a bad page never stops the rest).
"""
import argparse
import io
import json
import os
import sys
import tempfile
from pathlib import Path

# pdf2md_local lives in the CLI's environment, not in this venv: append (never prepend) its parent so the venv's own
# numpy / pillow / paddle packages win.
_parent = os.environ.get("PDF2MD_PACKAGE_PARENT")
if _parent and _parent not in sys.path:
    sys.path.append(_parent)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from pdf2md_local import convert as formatting  # noqa: E402

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--first", type=int, default=1)
    ap.add_argument("--last", type=int, default=10**6)
    ap.add_argument("--server", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--page-list", help="internal diagnostic selection: comma-separated physical page numbers")
    ap.add_argument("--diagnostics", action="store_true", help="retain layout JSON and rendered pages for review")
    ap.add_argument("--no-safety", action="store_true", help="internal baseline only: disable source-backed guards")
    ap.add_argument("--scale", type=float, default=2.5,
                    help="page render scale (2.5 = 180 dpi)")
    a = ap.parse_args()

    import pypdfium2
    from paddleocr import PaddleOCRVL

    pdf = pypdfium2.PdfDocument(a.pdf)
    last = min(a.last, len(pdf))
    pages = list(range(max(a.first, 1), last + 1))
    if a.page_list:
        pages = sorted(set(int(n) for n in a.page_list.split(',')))
        if not pages or min(pages) < 1 or max(pages) > len(pdf):
            ap.error('--page-list contains an out-of-range page')
    raw_outline = []
    for bookmark in pdf.get_toc():
        dest = bookmark.get_dest()
        index = dest.get_index() if dest else -1
        raw_outline.append([bookmark.level+1,bookmark.get_title(),index+1 if index is not None and index >= 0 else -1])
    outline = formatting.normalize_pdf_outline(raw_outline,len(pdf))
    pipe = PaddleOCRVL(pipeline_version="v1.6", vl_rec_backend="llama-cpp-server", vl_rec_server_url=a.server,
                       device="cpu")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for n, page_no in enumerate(pages, 1):
            try:
                img = Path(tmp) / f"p{page_no}.png"
                page = pdf[page_no - 1]
                rendered = page.render(scale=a.scale).to_pil().convert("RGB")
                rendered.save(img)
                dst = out / f"page_{page_no:04d}"
                dst.mkdir(exist_ok=True)
                reports = []
                for res in pipe.predict(str(img)):
                    if not a.no_safety:
                        high_res = [None]
                        def crop(bbox, pad=6, high=False):
                            if high and high_res[0] is None:
                                high_res[0] = page.render(scale=max(4.0,a.scale)).to_pil().convert('RGB')
                            image = high_res[0] if high else rendered
                            width = res['width'][0] if isinstance(res['width'],list) else res['width']
                            height = res['height'][0] if isinstance(res['height'],list) else res['height']
                            sx,sy = image.width/width,image.height/height
                            return image.crop((max(0,int((bbox[0]-pad)*sx)),max(0,int((bbox[1]-pad)*sy)),
                                               min(image.width,int((bbox[2]+pad)*sx)),min(image.height,int((bbox[3]+pad)*sy))))
                        def native_text(bbox):
                            if page.get_rotation() or res['model_settings'].get('use_doc_preprocessor'):
                                return ''
                            try:
                                w,h = page.get_size()
                                textpage = page.get_textpage()
                                try:
                                    return textpage.get_text_bounded(left=max(0,bbox[0]/a.scale-1),
                                        right=min(w,bbox[2]/a.scale+1),bottom=max(0,h-bbox[3]/a.scale-1),
                                        top=min(h,h-bbox[1]/a.scale+1))
                                finally:
                                    textpage.close()
                            except (RuntimeError,ValueError):
                                return ''
                        def retry_formula(bbox,pad):
                            request_image = io.BytesIO()
                            crop(bbox,pad,high=True).save(request_image,format='PNG')
                            import urllib.request
                            with urllib.request.urlopen(formatting.api_base(a.server)+'/models',timeout=10) as response:
                                model = json.load(response)['data'][0]['id']
                            return formatting.openai_chat(a.server,model,None,'Formula Recognition:',120,
                                                         [request_image.getvalue()],max_out=2048,require_complete=True)
                        def repeated_margin(content):
                            key = formatting.heading_key(content)[1]
                            for neighbor in (page_no-2,page_no-1,page_no+1,page_no+2):
                                if not 1 <= neighbor <= len(pdf):
                                    continue
                                other = pdf[neighbor-1]
                                try:
                                    w,h = other.get_size()
                                    tp = other.get_textpage()
                                    try:
                                        text = tp.get_text_bounded(left=0,bottom=h*.93,right=w,top=h)
                                    finally:
                                        tp.close()
                                finally:
                                    other.close()
                                if key in formatting.heading_key(text)[1]:
                                    return True
                            return False
                        if not res['model_settings'].get('use_doc_preprocessor'):
                            reports.append(formatting.protect_paddle_result(res,native_text,crop,retry_formula,
                                                                          page_no,outline,repeated_margin))
                    res.save_to_markdown(save_path=str(dst))
                    if a.diagnostics:
                        res.save_to_json(save_path=str(dst))
                (dst/'safety.json').write_text(json.dumps(reports,indent=2))
                if a.diagnostics:
                    import shutil
                    shutil.copy2(img, dst / 'source-page.png')
                page.close()
                print(f"PAGE {n}/{len(pages)}", flush=True)
            except Exception as e:  # noqa: BLE001 - one unreadable page must not lose the rest
                print(f"ERROR page {page_no}: {e.__class__.__name__}: {str(e)[:200]}", flush=True)
    pdf.close()


if __name__ == "__main__":
    sys.exit(main())
