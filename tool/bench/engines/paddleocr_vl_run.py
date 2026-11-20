# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["paddlepaddle==3.2.1", "paddleocr[doc-parser]==3.7.0"]
# ///
"""PaddleOCR-VL-1.6 pipeline (PaddlePaddle's layout model on CPU + the VLM served by llama.cpp's llama-server).

PADDLE_VL_URL points at the llama-server (default http://127.0.0.1:18080/v1). Raw output, no post-processing.
"""
import json, os, sys, tempfile, time
from pathlib import Path

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
ITEMS = json.load(open(sys.argv[1]))
from paddleocr import PaddleOCRVL

pipe = PaddleOCRVL(pipeline_version="v1.6", vl_rec_backend="llama-cpp-server",
                   vl_rec_server_url=os.environ.get("PADDLE_VL_URL", "http://127.0.0.1:18080/v1"), device="cpu")
for i, it in enumerate(ITEMS, 1):
    t = time.time()
    out = Path(it["out"])
    out.parent.mkdir(parents=True, exist_ok=True)
    text = ""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for res in pipe.predict(it["pdf"]):
                res.save_to_markdown(save_path=tmp)
            text = "\n\n".join(p.read_text(encoding="utf-8") for p in sorted(Path(tmp).glob("*.md")))
    except Exception as e:  # noqa: BLE001
        print(f"  FAILED {Path(it['pdf']).name}: {e.__class__.__name__}: {str(e)[:160]}", flush=True)
    out.write_text(text, encoding="utf-8")
    print(f"[{i}/{len(ITEMS)}] {Path(it['pdf']).name} {time.time() - t:.1f}s", flush=True)
