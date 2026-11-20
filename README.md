# PDF to Markdown for Obsidian

Convert PDFs into Obsidian Markdown **entirely on your own machine**: maths as `$inline$` and `$$` blocks that
Obsidian renders, figures, tables, headings, and whole books split into linked chapter notes. Nothing is sent to any
online service unless you point it at one.

It ships as an Obsidian plugin and as a standalone command-line tool (`pdf2md`), with two interchangeable engines.

## Engines

| | **PaddleOCR-VL-1.6** | **Docling + a vision model** |
|---|---|---|
| How | Paddle's layout model finds the blocks on each page; a 0.9B vision-language model served by [llama.cpp](https://github.com/ggml-org/llama.cpp) reads each one | [Docling](https://github.com/docling-project/docling) parses the layout; a chat vision model repairs equations and rewrites maths paragraphs as LaTeX |
| Model server | `llama-server` (no Ollama) | Ollama, or any OpenAI-compatible server |
| Maths | read by the model, inline and display | repaired and validated by the tool |
| Figure captions, prose cleanup | no | optional |
| In my tests | best on maths (93% vs 84%), about 3 GB of VRAM, no Ollama; weaker on tables and headers/footers | better on tables and headers/footers, weaker on scans; needs a 12B-class model for best results |

Both feed the same finishing steps: LaTeX checking, heading tidy-up, optional links to the PDF page, chapter
splitting, and resumable, never-overwriting output.

## What you get

- **Maths in Obsidian syntax.** Display equations as `$$` blocks on their own lines, inline maths as `$…$`.
- **Checked LaTeX.** Maths is validated before it is written. With the Docling engine, an equation the model cannot
  produce validly falls back to a picture instead of broken LaTeX, and one that looks cut off gets a collapsed
  "check this equation against the PDF" callout holding its source picture.
- **Links back to the PDF.** A `[[book.pdf#page=N|p. N]]` link under each heading when the PDF is in your vault.
- **Books.** *Split books into chapters* writes a folder: an index, front matter, one note per chapter with
  Previous / Contents / Next links, and the bibliography and index as their own notes.
- **Batch.** Convert several PDFs, a whole folder, or a page range, from the plugin or the command line.
- **Tables** become Markdown tables (tables with merged cells stay as HTML, which Obsidian also renders).
- **Safe on a vault.** It only creates new files, never overwrites one, resumes after an interruption, and a page that
  cannot be read is retried and then marked in the note instead of stopping the run.

## Requirements

| | |
|---|---|
| Obsidian | desktop, 1.4 or newer (the plugin starts local processes, so it is desktop-only) |
| [uv](https://docs.astral.sh/uv/) | installs Python 3.12 and every Python dependency automatically on first run |
| [llama.cpp](https://github.com/ggml-org/llama.cpp) | for the PaddleOCR-VL engine: you need its `llama-server` (build it, or use a release) |
| Hardware | a GPU is strongly recommended. PaddleOCR-VL needs about 3 GB of VRAM; the Docling engine with a 12B vision model about 10 GB |
| Disk | roughly 1 GB for the PaddleOCR-VL model plus a few GB of Python packages on first use |

Tested on Linux with an NVIDIA RTX 5060 Ti (16 GB). macOS and Windows are untested.

### Setting up PaddleOCR-VL with llama.cpp (no Ollama)

```bash
# 1. build llama-server once (CUDA shown; see llama.cpp's docs for Metal / Vulkan / CPU)
git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp
cmake -B build -DGGML_CUDA=ON && cmake --build build -j --target llama-server
export PATH="$PWD/build/bin:$PATH"

# 2. download the model (PaddleOCR-VL-1.6, Apache-2.0, about 1 GB) and start the server
tool/serve-paddleocr-vl.sh            # port 8111; stop it with Ctrl-C

# 3. convert
tool/pdf2md book.pdf --engine paddleocr-vl
```

Or let the tool start and stop the server for you: `--serve-command "$(realpath tool/serve-paddleocr-vl.sh)"`. In
the plugin, choose the PaddleOCR-VL engine and press *Use bundled script*.

## Install the plugin

**From a release.** Download `main.js`, `manifest.json` and `styles.css` from the
[latest release](../../releases/latest) into `<your vault>/.obsidian/plugins/pdf2md/`, then enable
*PDF to Markdown (Docling + Ollama)* under Settings → Community plugins.

**With [BRAT](https://github.com/TfTHacker/obsidian42-brat).** Add this repository's address as a beta plugin.

**From source.** `npm install && npm run build`, then copy the three files as above.

## Use

- Right-click a PDF → **Convert to Markdown**, or right-click a folder → **Convert all N PDFs**, or select several
  PDFs → **Convert N PDFs to Markdown**.
- Command palette: **Convert PDF to Markdown**, **Convert PDF pages…** (asks for a range such as `10-40`),
  **Convert several PDFs…** (a searchable checklist), **Cancel**, **Show the last log**.

Progress, with an estimate for long books, is shown in the status bar. A single conversion opens the resulting note
(or the book's index) when it finishes. The first run downloads Python packages and models and takes several minutes.

## Command line

```bash
tool/pdf2md paper.pdf                              # paper.md next to the PDF (Docling engine, Ollama)
tool/pdf2md a.pdf b.pdf c.pdf -o out/              # a batch, one after another
tool/pdf2md book.pdf --engine paddleocr-vl --split-chapters
tool/pdf2md book.pdf --pages 60-79                 # only some pages
tool/pdf2md paper.pdf --api openai --host http://localhost:8080 --vision-model any-name   # llama-server, LM Studio, vLLM
tool/pdf2md --help
```

If the output lands inside an Obsidian vault (a folder with `.obsidian`), the tool can run a backup command of your
choice first: set `PDF2MD_BACKUP_COMMAND` or pass `--backup-command`. Nothing is run by default.

## How well does it work?

I ran the engines the Obsidian plugins in this space are built on, plus this tool's two engines, on the same 46
single-page PDFs: a seeded sample of [olmOCR-bench](https://huggingface.co/datasets/allenai/olmOCR-bench) covering
all 8 of its categories (350 tests), scored with the benchmark's official scorer. Scores are % of tests passed.

| Engine (used by) | arXiv math | Tables | Multi-column | Old scans | Overall | s/PDF |
|---|---|---|---|---|---|---|
| Marker 2, balanced (OCR-AI) | 84.2 | 84.0 | 76.2 | 45.2 | **79.8** | 8 |
| MinerU 4, standard tier (MinerU plugins) | 84.2 | 60.0 | 76.2 | 45.2 | **76.1** | 3 |
| PaddleOCR-VL-1.6 pipeline, raw | 93.0 | 64.0 | 71.4 | 41.9 | **73.4** | 5 |
| **This tool, PaddleOCR-VL engine** | 93.0 | 64.0 | 71.4 | 41.9 | **72.7** | 7 |
| **This tool, Docling engine + gemma4:12b** | 84.2 | 80.0 | 61.9 | 25.8 | **68.9** | 20 |
| Marker 2, fast | 64.9 | 52.0 | 71.4 | 22.6 | **60.7** | 1 |
| Docling, default (MinerU Parser) | 0.0 | 80.0 | 66.7 | 12.9 | **53.2** | 1 |
| granite-docling (Reflow's model) | 17.5 | 52.0 | 61.9 | 25.8 | **53.1** | 20 |
| PyMuPDF4LLM | 0.0 | 52.0 | 76.2 | 9.7 | **32.2** | 0 |
| MarkItDown (DocDrop, MinerU Parser) | 0.0 | 32.0 | 38.1 | 9.7 | **27.1** | 0 |

The overall column is the average over all 8 categories (the other four are in `tool/bench`). Times are per PDF on
one RTX 5060 Ti; engines that load once and loop (Marker, MinerU, raw Paddle) are faster per page than ones started
per PDF (this tool, 20 s with Ollama). The overall scores carry about ±4 to ±6 points of uncertainty.

What this does and does not show:

- **This tool is not the most accurate.** Marker 2 and MinerU 4 score higher overall, though the gaps to the
  PaddleOCR-VL engine are within the uncertainty. They are clearly ahead of the Docling engine.
- **Its strengths are narrower:** the PaddleOCR-VL engine has the best arXiv-maths score (93% against 84%), needs about
  3 GB of VRAM, and runs without Ollama. The Docling engine is strong on tables and on dropping headers and footers.
- **Post-processing does not change these scores** (72.7 against 73.4 for the raw pipeline, one or two tests). What the
  tool adds is Obsidian formatting: Markdown tables, `![[embeds]]`, page links, tidied headings, chapter notes.
- **Both engines are weak on old scans** (as is every engine here), and Docling's scan OCR is the main reason the
  Docling engine trails on scanned maths.
- **Not tested:** the cloud engines some plugins call (Mistral OCR, Mathpix, MinerU's remote service), GLM-OCR, and
  Reflow's in-browser ONNX build of granite-docling (I ran the same model through Docling).
- This is a small sample of one-page documents, and one of my own earlier MinerU runs was invalid (a command-line
  change) until fixed. Run `tool/bench/run_olmocr_bench.py` to reproduce, or to test your own documents.


## Limitations

- Models misread things. Check the equations you rely on, especially those flagged with a callout.
- The PaddleOCR-VL engine does not caption figures or clean prose, and tables with merged cells stay as HTML.
- Old scans are hard for every tool; expect errors.
- Docling's reading order on dense multi-column pages is its weak spot.
- The first conversion needs a network connection to download packages and models.

## Development

```bash
npm install
npm test            # plugin unit tests + a contract test against the tool's real --help
npm run test:py     # the tool's tests (standard library only; no models needed)
npm run build       # type-check and bundle main.js (the Python tool and helper scripts are embedded)
```

The benchmark harness is `tool/bench/run_olmocr_bench.py`; each competing engine has a small adapter in
`tool/bench/engines/`. The plugin writes its bundled copy of the tool into its own folder, so the scripts always
match the plugin version.

## License

[MIT](LICENSE). Docling and PaddleOCR-VL are open source (MIT and Apache-2.0); Ollama and the models you use have
their own licences.
