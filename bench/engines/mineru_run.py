# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["mineru==4.2.3"]
# ///
"""MinerU 4 (the engine behind the MinerU Converter and MinerU Parser plugins), stateless local parse in one batch.

Run `mineru-kit models download --tier standard` once first (about 2 GB). TIER (env) selects the quality tier,
default standard; telemetry is switched off.
"""
import json, os, subprocess, sys, tempfile, time
from pathlib import Path

os.environ.setdefault("MINERU_TELEMETRY_DISABLED", "1")
ITEMS = json.load(open(sys.argv[1]))
tier = os.environ.get("TIER", "standard")

with tempfile.TemporaryDirectory() as tmp:
    src, out = Path(tmp, "in"), Path(tmp, "out")
    src.mkdir()
    for n, it in enumerate(ITEMS):
        (src / f"d{n:04d}.pdf").symlink_to(Path(it["pdf"]).resolve())
    t = time.time()
    r = subprocess.run(["mineru-kit", "parse", *sorted(map(str, src.glob("*.pdf"))), "-o", str(out), "--tier", tier,
                        "--pages", "all"], capture_output=True, text=True)
    total = time.time() - t
    print(f"mineru-kit exit={r.returncode} {total:.0f}s", flush=True)
    if r.returncode != 0:
        print((r.stderr or r.stdout)[-1200:], flush=True)
    for n, it in enumerate(ITEMS):
        hits = sorted(out.glob(f"**/d{n:04d}*.md"))
        Path(it["out"]).parent.mkdir(parents=True, exist_ok=True)
        Path(it["out"]).write_text(hits[0].read_text(encoding="utf-8") if hits else "", encoding="utf-8")
        print(f"[{n + 1}/{len(ITEMS)}] {Path(it['pdf']).name} {total / len(ITEMS):.1f}s {'ok' if hits else 'NO OUTPUT'}", flush=True)
