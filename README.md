# PDF to Markdown for Obsidian

Turn maths and science PDFs into a linked Obsidian library, locally. Available as an **Obsidian plugin and CLI**.
Documents stay on your machine unless you configure a remote model server.

## Features

- Inline/display LaTeX, figures and tables.
- **Chapter splitting**, a contents index and Previous / Contents / Next links.
- PDF bookmarks guide heading hierarchy; page links connect notes to the source.
- Source-backed checks retry damaged equations or preserve a source image instead of guessing.
- Batch conversion, page ranges, checkpoint/resume and progress with ETA.
- New output files only—existing notes and source PDFs are not overwritten.

**Recommended:** PaddleOCR-VL-1.6 with **2.5 Balanced** resolution, served by llama.cpp; no Ollama required.
The tested setup used about **3 GB VRAM** on a **16 GB GPU**. An 8 GB card is expected to fit but is untested.
System RAM, concurrency and other GPU applications also affect memory use.

**Alternative engine:** Docling with Ollama or an OpenAI-compatible vision model.

## Requirements

| | |
|---|---|
| Obsidian | desktop, 1.4 or newer (the plugin starts local processes, so it is desktop-only) |
| [uv](https://docs.astral.sh/uv/) | installs Python 3.12 and every Python dependency automatically on first run |
| [llama.cpp](https://github.com/ggml-org/llama.cpp) | for the PaddleOCR-VL engine: you need its `llama-server` (build it, or use a release) |
| Hardware | a GPU is strongly recommended. PaddleOCR-VL needs about 3 GB of VRAM; the Docling engine with a 12B vision model about 10 GB |
| Disk | roughly 1 GB for the PaddleOCR-VL model plus a few GB of Python packages on first use |

Tested on Linux with an NVIDIA RTX 5060 Ti (16 GB). macOS and Windows are untested.

## Quick start

Install **uv** and a GPU-enabled **llama-server**, and add both to your `PATH`.

```bash
# Download the model and start the local server; leave this terminal running.
tool/serve-paddleocr-vl.sh

# In another terminal, from this repository:
tool/pdf2md book.pdf --engine paddleocr-vl --split-chapters
```

In the plugin, choose **PaddleOCR-VL**, **2.5 Balanced**, and **Split books into chapters**.
To let the tool manage the server, use `--serve-command "$(realpath tool/serve-paddleocr-vl.sh)"`.

## Install the plugin

Build the current source:

```bash
npm install && npm run build
```

Copy `main.js`, `manifest.json` and `styles.css` to `<vault>/.obsidian/plugins/pdf2md/`, then enable the plugin
under **Settings → Community plugins**.

Right-click PDFs or folders to convert, or use the command palette for conversion, page selection and cancellation.

The progress window shows pages, stage and ETA. **Run in background** keeps conversion running; click the status bar
to reopen progress. The first run downloads dependencies and models.

## Command line

```bash
tool/pdf2md a.pdf b.pdf --engine paddleocr-vl --split-chapters -o out/
tool/pdf2md book.pdf --engine paddleocr-vl --pages 60-79
tool/pdf2md --help
```

If the output lands inside an Obsidian vault (a folder with `.obsidian`), the tool can run a backup command of your
choice first: set `PDF2MD_BACKUP_COMMAND` or pass `--backup-command`. Nothing is run by default.

## Current benchmark

**PaddleOCR-VL-1.6, source guards enabled, scale 2.5 Balanced (180 dpi).** Measured on 46 single-page PDFs from a
seeded [olmOCR-bench](https://huggingface.co/datasets/allenai/olmOCR-bench) development subset, with 279 checks
across eight categories and the official scorer.

| Category | Passed | Score |
|---|---:|---:|
| arXiv maths | 52/57 | **91.2%** |
| Scanned maths | 35/57 | 61.4% |
| Tables | 19/25 | **76.0%** |
| Old scans | 12/31 | 38.7% |
| Headers and footers | 14/14 | **100.0%** |
| Multi-column | 18/21 | **85.7%** |
| Long/tiny text | 24/28 | 85.7% |
| Baseline checks | 45/46 | 97.8% |
| **Overall: mean across categories** | | **79.6% ±3.6 points (95% CI)** |

All 46 PDFs converted. Mean runtime: **6.48 seconds/PDF** on an RTX 5060 Ti (16 GB), including per-PDF startup.
This is a small, reused development set—not an independent accuracy test. Image fallbacks do not count as recognized
LaTeX. Only scale 2.5 with Paddle is covered. [Exact results and code hashes](tool/bench/results/source_guarded25_20261120.json).

### Book extraction validation

**Two complete mathematical/deep-learning books**, scale 2.5:

| Metric | Result |
|---|---:|
| Source pages represented | **1,093** |
| Notes, including both contents indexes | **44** |
| Image assets | **946** |
| Combined conversion time | **61 minutes** |
| Missing image targets / broken navigation links | **0 / 0** |
| Math-structure warnings | **0** |
| Formulas preserved as source images | **110** |

Chapter splitting, navigation and page coverage were checked. A 33-page sample was also compared with the PDFs.
This was not a full factual proofread; code extraction was not validated.

## Limitations

- OCR and heading errors can remain even with zero structural warnings. Check important equations against the PDF.
- Source-image fallbacks are **not editable LaTeX**. Merged-cell tables stay as HTML.
- Old scans and complex layouts remain difficult.
- Tested on Linux with a 16 GB GPU; 8 GB GPUs, macOS and Windows are untested.

## Development

```bash
npm install
npm test            # plugin unit tests + a contract test against the tool's real --help
npm run test:py     # the tool's tests (standard library only; no models needed)
npm run build       # type-check and bundle main.js (the Python tool and helper scripts are embedded)
```

Benchmark harness: `tool/bench/run_olmocr_bench.py`.

## License

[MIT](LICENSE). Docling and PaddleOCR-VL are open source (MIT and Apache-2.0); Ollama and the models you use have
their own licences.
