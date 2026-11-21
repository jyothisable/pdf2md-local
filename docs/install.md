# Installing pdf2md-local

Everything that installs or downloads is done by **you, in a terminal**. The Obsidian plugin only checks and uses it.

## Linux (tested)

1. **uv** — <https://docs.astral.sh/uv/getting-started/installation/>
2. The tool, its backend and llama.cpp:
   ```bash
   uv tool install git+https://github.com/jyothisable/pdf2md-local@0.6.0
   pdf2md setup       # worker environment, 1.8 GB model, and a prebuilt llama-server unless one is already on PATH
   pdf2md selftest    # converts a bundled sample and checks it
   ```
   `pdf2md setup` downloads the tested llama.cpp release (`b12606`) for your system (NVIDIA CUDA matching your
   driver, else Vulkan, else CPU), verifies the digest GitHub publishes and unpacks it in
   `~/.local/share/pdf2md-local/llama.cpp`. Use `--llama cuda|vulkan|cpu` to force one, or `--llama skip` to use your
   own build. `PDF2MD_LLAMA_TAG=bNNNNN` picks another release (untested); an asset without a published digest is
   refused unless you pass `--allow-unverified`. The model files come from a pinned Hugging Face revision and are
   checked by size and SHA-256.
3. Convert: `pdf2md serve` in one terminal, `pdf2md book.pdf --split-chapters` in another (or
   `--serve-command "pdf2md serve"` to let the tool start and stop the server).

`pdf2md setup` is safe to repeat; an interrupted download resumes (and restarts if the file changed upstream).

**Disk and memory:** about 4 GB of disk (1.8 GB model, 1.4 GB worker environment, up to 0.7 GB for the CUDA llama.cpp
build). With an NVIDIA GPU, about 3 GB of VRAM and 3.4 s/page (tested on a 16 GB card). On CPU only, about 3 GB of
RAM for the server and 22 s/page on 12 cores. Linux on ARM has no prebuilt llama.cpp; build it yourself (below).

**Building llama.cpp yourself** (if no prebuilt fits): see <https://github.com/ggml-org/llama.cpp>, then put
`llama-server` on `PATH` or set `LLAMA_SERVER=/path/to/llama-server`.

Environment variables: `PDF2MD_HOME` (data folder, default `$XDG_DATA_HOME/pdf2md-local` or
`~/.local/share/pdf2md-local`), `MODEL_DIR` (model files), `LLAMA_SERVER`, `PDF2MD_LLAMA_TAG`,
`PORT`/`SLOTS`/`CTX`/`NGL` (for `pdf2md serve`; defaults 8111/3/16384/99), `PDF2MD_PADDLE_SERVER`.

## Where files live

| What | Where |
|---|---|
| the `pdf2md` command | managed by uv (`uv tool dir`) |
| worker environment, llama.cpp, server log, speed estimate | `~/.local/share/pdf2md-local/` (`worker-venv/`, `llama.cpp/`, `logs/llama-server.log`, `rate.json`) |
| model files | `~/.cache/pdf2md/models/` |
| checkpoints | `~/.cache/pdf2md/<run>/`: removed after a successful run; kept when pages failed (run the same command again to retry them) or the run was interrupted (run it again to resume) |

## Update

```bash
uv tool install --force --reinstall git+https://github.com/jyothisable/pdf2md-local@X.Y.Z   # the release you want
pdf2md setup       # fetches anything the new release needs; already-present files are kept
pdf2md selftest
```

Use the same version as the Obsidian plugin: its **Check** button warns when they differ.

## Uninstall

```bash
uv tool uninstall pdf2md-local
rm -rf ~/.local/share/pdf2md-local ~/.cache/pdf2md     # worker environment, llama.cpp, model, checkpoints
```

In Obsidian, disable and remove the plugin under **Settings → Community plugins**. Your converted notes are left alone.

## macOS and Windows (untested)

`pdf2md setup` knows the official macOS (Metal) and Windows (CUDA/Vulkan/CPU) llama.cpp builds and the Python
dependencies publish wheels for those systems, so it may work, but **nobody has confirmed it**. Run
`pdf2md selftest` and report what happens in an issue.

## Troubleshooting

| Message | What to do |
|---|---|
| `missing worker environment` / `missing model files` | run `pdf2md setup` |
| `llama-server not found` | run `pdf2md setup` (downloads one), or set `LLAMA_SERVER` |
| `no PaddleOCR-VL server at …` | run `pdf2md serve`, or add `--serve-command "pdf2md serve"` |
| server exits immediately / out of memory | read `~/.local/share/pdf2md-local/logs/llama-server.log` (written when the tool or plugin starts the server; `pdf2md serve` run in a terminal prints its log to that terminal); lower `SLOTS` or `CTX`; close other GPU programs |
| port already in use | set `PORT=…` for `pdf2md serve` and pass `--paddle-server http://127.0.0.1:PORT/v1` |
| Plugin: “could not run pdf2md” | Obsidian may not see `~/.local/bin`; set the full path in the plugin settings |
| Plugin: nothing runs on Linux | Flatpak and Snap builds of Obsidian are sandboxed and cannot start programs from your system; use the AppImage or `.deb`/`.rpm` |
| `another conversion of this PDF is running` | wait for it, or cancel it; a lock left by a crashed run is taken over automatically |
| `pages_failed` in a note | run the same command again (same options): only those pages are re-read |

Run `pdf2md selftest` for an end-to-end test, and `pdf2md check` first: it reports the worker environment, model files, `llama-server`, the server and the GPU.
