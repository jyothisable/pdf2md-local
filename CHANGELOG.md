# Changelog

## 0.4.4

- The hidden `<!-- pdf2md:page=N -->` comments are no longer written to notes. `--page-markers` (plugin: **Keep hidden page markers**) restores them.
- `scripts/strip_page_markers.py` removes them from notes converted earlier (dry run by default; `--apply` backs up first).

## 0.4.3

- Policy self-check with `eslint-plugin-obsidianmd`: type-safe parsing of the setup report, window timers, no inline styles, and no top-level settings heading.
- The output folder is created through the Obsidian API, normalised, and must stay inside the vault (`..` and absolute paths are rejected).
- `minAppVersion` is 1.5.7 (the oldest version with every API the plugin uses).
- README lists the files the tool touches outside the vault.

## 0.4.2

- Progress window: the Details section stays open (and keeps its scroll position) while the view refreshes.

## 0.4.1

- Fix: the page count was read with a library that only the worker environment has, so conversions without `--pages` (the plugin default) reported no progress or ETA and the whole PDF ran as one unbounded slice. It now uses `pypdf`.

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
