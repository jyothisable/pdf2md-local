# Changelog

## 0.4.0

- Renamed to **pdf2md-local**; general command-line tool first, Obsidian plugin second.
- New `pdf2md setup`, `pdf2md check [--json]` and `pdf2md serve`; installation is now an explicit terminal step.
- **The plugin no longer installs anything**: it calls the installed `pdf2md` and reports what is missing.
- New `--flavor plain|obsidian|auto` output.
- PaddleOCR-VL-1.6 with source-backed safeguards and PDF-bookmark heading repair.
- **Removed** the Docling + Ollama engine and its settings (`--engine`, `--api`, `--model`, `--clean`, …).
- Plugin id is now `pdf2md-local`; old settings are not imported automatically.

## 0.3.0 and earlier

Docling + Ollama and PaddleOCR-VL engines, progress window, batch conversion and page ranges.
