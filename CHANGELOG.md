# Changelog

## 0.7.0

- **Choose chapters:** `pdf2md outline [--json]` lists the PDF's bookmarks with page ranges; `--pages` (and `pdf2md info --pages`) accepts lists such as `10-40,60-80,95`. Plugin: **Convert chapters…** shows the bookmark tree with checkboxes, page ranges and estimated times.
- **Equation numbers kept:** display equations keep their printed number as `\tag{1.2}`.
- **Cross-reference links** (`--cross-links`, default on for Obsidian output): numbered equations, figures, tables and theorem-like statements get block anchors, and references such as "Figure 1.2", "(1.2)", "Eq. (3.4)", "Theorem 2.1", "Section 1.2" and "Chapter 3" link to them, across chapter notes.
- **The PDF's own links** (`--pdf-links`) are used first where the PDF has them, with text-pattern matching for the rest; the PDF's web links are kept as Markdown links.
- **Theorem callouts** (`--callouts`, default on for Obsidian output): Theorem, Lemma, Proposition, Corollary, Definition, Example, Remark, Exercise and Proof become callouts.
- Plugin: settings for callouts and cross-reference links; the finished-job report shows how many callouts and links were made.

## 0.6.0

**Fixes**
- Cancelling (plugin, Ctrl-C or SIGTERM) now stops the page worker and any model server the run started; the plugin follows up with SIGKILL after 10 s (`taskkill /T` on Windows) and cancels on quit.
- Pages that could not be read are no longer cached as done: they are marked, listed in `pages_failed` (also in `00 Index.md`), the checkpoint is kept, and running the same command again re-reads only those pages.
- Safe unpacking of the llama.cpp archive also rejects symlinks and hard links that point outside the install folder.
- Pinned downloads: the model comes from a fixed Hugging Face revision checked by size and SHA-256; llama.cpp is the tested release `b12606` (`PDF2MD_LLAMA_TAG` overrides) and an asset without a published digest is refused unless `--allow-unverified`. Interrupted downloads resume only if the file is unchanged, and truncated downloads are refused.
- The worker uses only its own environment's packages and reads/writes UTF-8; a failing worker's last output lines are shown.
- The backup runs before anything (including figures) is written; frontmatter titles are escaped for any characters; `--no-cache` temp folders are removed on failure; two runs of the same PDF can no longer share a checkpoint (lock file); the server log moved to `~/.local/share/pdf2md-local/logs/`.
- Plugin: a second conversion can no longer start while the first is being prepared; results open in symlinked vaults (or a notice says where they were written); folder-creation errors are shown; the output folder cannot be inside `.obsidian`; the progress window no longer rebuilds every second; sentence-case labels.

**New**
- `pdf2md --version`, `version` in `check --json`, and a plugin warning when the tool and plugin versions differ.
- One `RESULT <json>` line per converted PDF.
- Plugin: a needs-review report (unread pages, formulas kept as pictures, math warnings, with links to the note and PDF page), clickable results for batches, and **Retry failed pages**.
- Docs: hardware and CPU-only speed, disk use, where files live, update, uninstall, Flatpak/Snap note.

**Internal:** `convert.py` split into `latex`, `guards`, `outline`, `notes` and `runner` modules.

## 0.5.0

- **One-command setup:** `pdf2md setup` also downloads a prebuilt, checksum-verified llama.cpp (`llama-server`) for your system when none is found (`--llama auto|cuda|vulkan|cpu|skip`, `--llama-only`).
- **`pdf2md selftest`:** converts a bundled CC0 two-page sample and checks the result. `examples/` has its source and the expected output.
- **Estimates before converting:** `pdf2md info [--json]` reports pages and estimated time, learning your machine's speed from past runs. The plugin shows it (and asks to confirm above a threshold, default 5 minutes) before every conversion.
- **Plugin setup window:** **Check** lists what is ready or missing, with copyable commands.

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
