# PDF to Markdown for Obsidian

Convert PDFs into Obsidian Markdown **entirely on your own machine**: maths as `$inline$` and `$$` blocks that
Obsidian renders, figures, tables, headings, and whole books split into linked chapter notes. Nothing is sent to any
online service unless you point it at one.

It ships as an Obsidian plugin and as a standalone command-line tool (`pdf2md`), with two interchangeable engines.

## Why choose it for a local 8–16 GB GPU?

**A strong fit for private, maths-heavy reading—not a claim to be the best parser for every document.**

- **Small OCR model, not a large general-purpose chatbot.** PaddleOCR-VL-1.6 is a 0.9B model served directly by
  llama.cpp. The tested server setup used roughly 3 GB of VRAM; layout analysis runs on the CPU.
- **8 GB VRAM:** the measured footprint suggests useful headroom, without needing a 12B model or Ollama. This is
  an estimate from a 16 GB GPU, **not a test on an 8 GB card**. Resolution, context size, concurrency and other GPU
  applications can increase memory use. Reduce server slots if memory is tight.
- **16 GB VRAM:** this is the tested configuration. The small Paddle model leaves room rather than requiring the
  larger Docling-plus-Gemma setup, which used about 10 GB for its model server.
- **Built for studying locally.** Editable inline/display LaTeX, source-page links, chapter notes, batch conversion,
  checkpoints, and a progress window with background operation and ETA—not just an OCR text dump.
- **No per-page service charge or document upload** when using the local setup. Downloads, GPU time, electricity
  and system RAM are still required. The test machine had about 30 GB of system RAM; VRAM is not the whole budget.

Marker and MinerU are strong local alternatives and scored higher overall in the initial comparison below.
Our strongest measured category was arXiv maths. Choose based on your documents, memory budget and workflow;
this project does not establish an accuracy or speed advantage on every 8 GB or 16 GB machine.

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

A progress window shows a bar, the current page, the stage and the time left. **Run in background** closes it while the
conversion continues; the status bar shows the percentage and time left, and clicking it brings the window back (or
use the command *Show conversion progress*). When a conversion finishes with the window closed, the resulting note (or
the book's index) opens. The first run downloads Python packages and models and takes several minutes.

## Command line

```bash
tool/pdf2md paper.pdf                              # paper.md next to the PDF (Docling engine, Ollama)
tool/pdf2md a.pdf b.pdf c.pdf -o out/              # a batch, one after another
tool/pdf2md book.pdf --engine paddleocr-vl --split-chapters
tool/pdf2md book.pdf --engine paddleocr-vl --paddle-scale 4   # higher resolution for table-heavy PDFs
tool/pdf2md book.pdf --pages 60-79                 # only some pages
tool/pdf2md paper.pdf --api openai --host http://localhost:8080 --vision-model any-name   # llama-server, LM Studio, vLLM
tool/pdf2md --help
```

If the output lands inside an Obsidian vault (a folder with `.obsidian`), the tool can run a backup command of your
choice first: set `PDF2MD_BACKUP_COMMAND` or pass `--backup-command`. Nothing is run by default.

## How well does it work?

I ran the engines the Obsidian plugins in this space are built on, plus this tool's two engines, on the same 46
single-page PDFs: a seeded sample of [olmOCR-bench](https://huggingface.co/datasets/allenai/olmOCR-bench) covering
all 8 of its categories (279 tests), scored with the benchmark's official scorer. Scores are % of tests passed.

The improved configurations appear first, followed by the initial comparison runs. Every row uses the same
46-page set. See the follow-up section for the separate, larger table/multi-column comparison.

| Engine (used by) | arXiv math | Tables | Multi-column | Old scans | Overall | s/PDF |
|---|---|---|---|---|---|---|
| **This tool, improved Paddle — balanced, scale 2.5** | **94.7** | **76.0** | **81.0** | **45.2** | **80.2** | **7** |
| **This tool, improved Paddle — tables priority, scale 4.0** | **91.2** | **88.0** | **76.2** | **41.9** | **77.6** | **8** |
| Marker 2, balanced (OCR-AI) | 84.2 | 84.0 | 76.2 | 45.2 | **79.8** | 8 |
| MinerU 4, standard tier (MinerU plugins) | 84.2 | 60.0 | 76.2 | 45.2 | **76.1** | 3 |
| PaddleOCR-VL-1.6 pipeline, raw | 93.0 | 64.0 | 71.4 | 41.9 | **73.4** | 5 |
| This tool, initial PaddleOCR-VL engine | 93.0 | 64.0 | 71.4 | 41.9 | **72.7** | 7 |
| **This tool, Docling engine + gemma4:12b** | 84.2 | 80.0 | 61.9 | 25.8 | **68.9** | 20 |
| Marker 2, fast | 64.9 | 52.0 | 71.4 | 22.6 | **60.7** | 1 |
| Docling, default (MinerU Parser) | 0.0 | 80.0 | 66.7 | 12.9 | **53.2** | 1 |
| granite-docling (Reflow's model) | 17.5 | 52.0 | 61.9 | 25.8 | **53.1** | 20 |
| PyMuPDF4LLM | 0.0 | 52.0 | 76.2 | 9.7 | **32.2** | 0 |
| MarkItDown (DocDrop, MinerU Parser) | 0.0 | 32.0 | 38.1 | 9.7 | **27.1** | 0 |

The overall column is the average over all 8 categories (the other four are in `tool/bench`). Times are per PDF on
one RTX 5060 Ti; engines that load once and loop (Marker, MinerU, raw Paddle) are faster per page than ones started
per PDF (this tool, 20 s with Ollama). The overall scores carry about ±4 to ±6 points of uncertainty.

**Pick scale 2.5 for balanced use, or scale 4.0 when tables take priority.** Scale 4.0 improves table accuracy but
does not win overall. Its 88.0% above is on the original 25 table checks; the 89.1% later in this README combines
55 table checks across two page sets and must not be substituted into this table.

What this does and does not show:

- **The initial version was not the most accurate.** Marker 2 and MinerU 4 scored higher overall, though the gaps to the
  PaddleOCR-VL engine are within the uncertainty. They are clearly ahead of the Docling engine.
- **Its strengths are narrower:** the PaddleOCR-VL engine has the best arXiv-maths score (93% against 84%), needs about
  3 GB of VRAM, and runs without Ollama. The Docling engine is strong on tables and on dropping headers and footers.
- **The initial post-processing made little difference to these scores** (72.7 against 73.4 for the raw pipeline).
  Later improvements are measured separately below. The tool also adds Obsidian formatting: Markdown tables,
  `![[embeds]]`, page links, tidied headings, chapter notes.
- **Both engines are weak on old scans** (as is every engine here), and Docling's scan OCR is the main reason the
  Docling engine trails on scanned maths.
- **Not tested:** the cloud engines some plugins call (Mistral OCR, Mathpix, MinerU's remote service), GLM-OCR, and
  Reflow's in-browser ONNX build of granite-docling (I ran the same model through Docling).
- This is a small sample of one-page documents, and one of my own earlier MinerU runs was invalid (a command-line
  change) until fixed. Run `tool/bench/run_olmocr_bench.py` to reproduce, or to test your own documents.


### Follow-up: resolution and table-text improvements

Two formatting fixes recover text that the evaluator—and Obsidian readers—otherwise see as literal LaTeX:
bare unit macros such as `250 \mu M` become `250 µM`, and simple chemical/typographic spans such as `$CO_2$`
become Unicode text. Ordinary mathematical expressions remain LaTeX.

On the same 46-page set, these fixes plus rendering at **scale 2.5 (180 dpi)** changed the results:

| Configuration | arXiv maths | Tables | Multi-column | Overall |
|---|---:|---:|---:|---:|
| Initial Paddle engine | 93.0% | 64.0% | 71.4% | 72.7% |
| Improved Paddle engine, scale 2.5 | 94.7% | 76.0% | 81.0% | 80.2% |
| Marker balanced, reference run | 84.2% | 84.0% | 76.2% | 79.8% |

**80.2% versus 79.8% is not evidence that this tool beats Marker:** the overall uncertainty is approximately
±5 points, and we repeatedly used this small set during development.

We then compared resolutions on the original 12 table/multi-column pages and 12 additional pages. The additional
pages were initially separate from tuning, but were subsequently used to select the resolution too; they are
therefore **validation/development data, not an untouched final test set**. Combined results cover 55 table tests
and 47 multi-column tests:

| Render scale | Resolution | Tables | Multi-column | 60% tables + 40% multi-column |
|---|---:|---:|---:|---:|
| **4.0** | **288 dpi** | **89.1%** | 72.3% | **82.4** |
| **2.5** | **180 dpi** | 83.6% | **76.6%** | **80.8** |
| 3.25 | 234 dpi | 81.8% | 74.5% | 78.9 |
| 3.5 | 252 dpi | 81.8% | 74.5% | 78.9 |
| 3.75 | 270 dpi | 74.5% | 72.3% | 73.7 |
| 2.75 | 198 dpi | 72.7% | 72.3% | 72.6 |

**Recommendation:** keep **2.5 as the balanced default**; choose **4.0 when tables matter more than multi-column
text** (`--paddle-scale 4`, or *Page resolution → Tables* in the plugin). Higher resolution is not monotonically
better. The table-priority weighting is a preference, not a standard benchmark metric, and the lead is small.
This does not establish the optimal scale for every PDF, GPU or language.

The investigated failures did not show missing table tails from token truncation. They included mistranscribed
names, incorrectly merged rows and literal LaTeX units. Several multi-column test failures were text/OCR
mismatches rather than incorrect reading order. Disabling layout-block merging did not improve the tested pages.
No benchmark-specific names, expected answers or spelling corrections were added to the converter.

## Final comparison: when this tool is the better fit

**Choose this tool when your goal is to turn maths-heavy PDFs into a usable local Obsidian library—not simply
to obtain the highest general-purpose OCR score.** Its advantage is the combination of a small local model,
strong measured maths recognition and a book-oriented workflow.

| Your priority | Why choose PDF2MD? | What the evidence does not establish |
|---|---|---|
| Maths-heavy papers | The improved balanced configuration passed **94.7% of the 57 arXiv maths tests**, versus 84.2% for the tested Marker and MinerU configurations. | Not a claim about all equations, handwriting or languages; this subset was used during development. |
| Table-heavy reading | The 288-dpi preset reached **89.1% on 55 table checks** across two page sets, versus 83.6% at 180 dpi. | Marker was not rerun on the additional pages, so this pooled result cannot establish a table-accuracy win over Marker. |
| An 8–16 GB local GPU | PaddleOCR-VL is a 0.9B model; the tested llama.cpp server used roughly **3 GB of VRAM**, without a large chatbot or Ollama. | Tested on a 16 GB card only. Fit on 8 GB is an inference from the footprint, not a measured speed or compatibility result. |
| Studying whole books in Obsidian | Chapter notes, linked contents, Previous/Contents/Next navigation, PDF-page links, figures and editable maths are integrated into one workflow. | Other plugins may offer overlapping features; this is not a claim that these features are exclusive. |
| Long local jobs | Batch conversion, checkpoint/resume, a progress window, ETA and background operation make lengthy conversions manageable. | UI behaviour and non-Linux platforms have not been comprehensively tested. |
| Privacy and ownership | Local inference, no per-page API bill, MIT-licensed project code, and a standalone CLI as well as an Obsidian plugin. | Other open-source tools also run locally. Model downloads, hardware and electricity still have costs; dependencies have their own licences. |

### Which should you choose?

- **PDF2MD:** a strong choice for a local, maths-focused Obsidian workflow on consumer hardware, especially when
  chapter organisation and source links matter as much as extraction.
- **Marker balanced:** a strong alternative for general extraction. It led the initial overall comparison and
  remains effectively tied with the improved balanced PDF2MD configuration within the uncertainty.
- **MinerU standard:** worth considering when throughput matters; it was the fastest high-quality engine in
  the initial local comparison.
- **Simple text-layer converters:** preferable for uncomplicated text documents when milliseconds and minimal
  setup matter more than equations, scans and document structure.

**Bottom line:** PDF2MD's strongest case is *local maths recognition plus study-ready Obsidian output with a small
GPU footprint*. It is not proven to be the best OCR engine overall. The improvements bring its balanced score into
the same range as the strongest measured alternative, while its workflow makes the results useful for reading and
linking books. Judge it on your own documents before committing a large library.

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
