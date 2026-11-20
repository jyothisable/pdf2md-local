# PDF to Markdown for Obsidian (Docling + Ollama)

Convert a PDF into Obsidian Markdown **entirely on your own machine**. [Docling](https://github.com/docling-project/docling)
reads the layout, tables and figures; a local [Ollama](https://ollama.com) model fixes what plain extraction gets
wrong, above all **mathematics**: equations come out as `$inline$` and multi-line `$$` blocks that Obsidian renders.

Nothing is sent to any online service. It ships as an Obsidian plugin and as a standalone command-line tool.

## What you get

- **Maths in Obsidian syntax.** Display equations as `$$` blocks on their own lines; inline maths as `$…$`. Equation
  numbers become `\tag{n}`. Equations that cannot be decoded are kept as a cropped picture instead of broken LaTeX.
- **Validated LaTeX.** Every equation and inline span is checked before it is written. A model answer that drops words
  or breaks the LaTeX is rejected, retried once with a stricter prompt, and otherwise left as plain text.
- **Figures** are extracted next to the note and embedded as `![[image.png]]`, with a short generated description.
- **Books**: with *Split books into chapters* you get a folder with a linked index, front matter, one note per
  chapter (with Previous / Contents / Next links) and the bibliography and index as their own notes.
- **Headings tidied**: numbered headings get the right level, table-of-contents lines and code comments that were
  mistaken for headings become plain text.
- **Safe to run on a vault.** It only creates new files and never overwrites one. An interrupted run resumes from the
  last finished 40-page slice. A page that fails to convert is retried and, if it still fails, marked in the note
  instead of stopping the run.

## Requirements

| | |
|---|---|
| Obsidian | desktop, 1.4 or newer (the plugin is desktop-only: it starts a local process) |
| [uv](https://docs.astral.sh/uv/) | installs Python 3.12 and Docling automatically on first run |
| [Ollama](https://ollama.com) | running locally, with a vision-capable model, e.g. `ollama pull gemma4:12b` |
| Hardware | a GPU with about 16 GB is strongly recommended. Without Ollama the tool still works (Docling only) |
| Disk | a few GB for Docling's models on the first run |

Tested on Linux with an NVIDIA RTX 5060 Ti (16 GB). macOS and Windows are untested.

## Install

**From a release (manual).** Download `main.js`, `manifest.json` and `styles.css` from the
[latest release](../../releases/latest) into `<your vault>/.obsidian/plugins/pdf2md/`, then enable
*PDF to Markdown (Docling + Ollama)* under Settings → Community plugins.

**With [BRAT](https://github.com/TfTHacker/obsidian42-brat).** Add this repository's address as a beta plugin.

**From source.** `npm install && npm run build`, then copy `main.js`, `manifest.json` and `styles.css` as above.

## Use

- Right-click a PDF in the file explorer → **Convert to Markdown (Docling + Ollama)**, or
- run **Convert PDF to Markdown** from the command palette (it uses the open PDF, or asks you to pick one).

Progress is shown in the status bar. When it finishes, the note (or the book's index) opens. Other commands:
*Cancel the running PDF conversion*, *Show the last PDF conversion log*.

The first run downloads Docling's models and sets up Python, which takes several minutes. Conversion speed depends
on your GPU: on the test machine a 552-page maths book took about 48 minutes and a 541-page one about 39 minutes.

### Settings

Ollama address and models, parallel requests (3 is the sweet spot on one GPU), vision on/off, chapter splitting,
output folder, and a few advanced options (prose cleanup mode, inline-maths mode, pages per slice, the `uv` path, extra
command-line flags).

## Command-line use

The same tool works without Obsidian:

```bash
tool/pdf2md paper.pdf                       # paper.md next to the PDF
tool/pdf2md book.pdf --split-chapters       # a folder of chapter notes
tool/pdf2md paper.pdf --pages 10-40         # only some pages
tool/pdf2md paper.pdf --no-llm              # Docling only
tool/pdf2md --help
```

If the output lands inside an Obsidian vault (a folder with `.obsidian`), the tool can run a backup command of your
choice first: set `PDF2MD_BACKUP_COMMAND` or pass `--backup-command`. Nothing is run by default.

## How it works

1. **Docling** parses the PDF (layout, tables, OCR, formulas to LaTeX, figures), in slices of 40 pages.
2. Equations are tidied and validated. Invalid ones are re-read from a crop of the page by the vision model.
3. Paragraphs that look mathematical (symbols, glyph names, characters lost in extraction) are re-read from the page
   image and rewritten with inline LaTeX, then accepted only if the words are preserved and every span is valid.
4. Prose cleanup runs only on chunks that show extraction damage, with maths, tables and code swapped for
   placeholders so the model cannot alter them.
5. Maths delimiters are normalised, headings tidied, and the note (or chapter notes) written without overwriting.

## How well does it work?

Measured on a 28-PDF subset of [olmOCR-bench](https://huggingface.co/datasets/allenai/olmOCR-bench) (12 arXiv-maths,
6 table, 6 multi-column and 4 header/footer pages; 145 tests) with its official scorer:

| | Overall | Maths | Tables | Reading order |
|---|---|---|---|---|
| Docling alone | 72.8% | 17.5% | 80.0% | 66.7% |
| This tool | **85.2%** | **84.2%** | 80.0% | 61.9% |

Read this honestly: it is a small subset, the scorer reports about ±5 points of uncertainty, and the gain is almost
entirely in maths. Tables and reading order are limited by Docling's layout model and are not improved (reading order
is within noise of Docling alone). Real books have no reference Markdown, so for those I checked structure and counts
(every link and figure resolves, maths delimiters balance, no failed pages) and read samples, not whole books.

## Limitations

- The vision model sometimes misreads a symbol; it is not a proof-reader. Check equations you rely on.
- Multi-column pages and complex tables can come out in the wrong order or flattened.
- A few paragraphs stay as plain text when the model's rewrite changes words.
- It needs a model that accepts images. Quality depends on the model you pick.

## Development

```bash
npm install
npm test            # plugin unit tests + a contract test against the tool's real --help
npm run test:py     # the tool's tests (standard library only; no Docling needed)
npm run build       # type-check and bundle main.js (the Python tool is embedded)
python3 tool/bench/run_olmocr_bench.py      # reproduce the benchmark (see the file's header)
```

The plugin writes its bundled copy of `tool/pdf_to_md.py` into its own folder, so the script always matches the
plugin version.

## License

[MIT](LICENSE). Docling is MIT-licensed; Ollama and the models you pull have their own licences.
