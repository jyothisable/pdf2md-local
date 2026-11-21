# Installing pdf2md-local

Everything that installs or downloads is done by **you, in a terminal**. The Obsidian plugin only checks and uses it.

## Linux (tested)

1. **uv** — <https://docs.astral.sh/uv/getting-started/installation/>
2. The tool, its backend and llama.cpp:
   ```bash
   uv tool install git+https://github.com/jyothisable/pdf2md-local
   pdf2md setup       # worker environment, ~1 GB model, and a prebuilt llama-server unless one is already on PATH
   pdf2md selftest    # converts a bundled sample and checks it
   ```
   `pdf2md setup` chooses the llama.cpp release for your system (NVIDIA CUDA matching your driver, else Vulkan, else
   CPU), verifies its checksum and unpacks it in `~/.local/share/pdf2md-local/llama.cpp`. Use `--llama cuda|vulkan|cpu`
   to force one, or `--llama skip` to use your own build.
3. Convert: `pdf2md serve` in one terminal, `pdf2md book.pdf --split-chapters` in another (or
   `--serve-command "pdf2md serve"` to let the tool start and stop the server).

`pdf2md setup` is safe to repeat; an interrupted download resumes.

**Building llama.cpp yourself** (if no prebuilt fits): see <https://github.com/ggml-org/llama.cpp>, then put
`llama-server` on `PATH` or set `LLAMA_SERVER=/path/to/llama-server`.

Environment variables: `PDF2MD_HOME` (worker environment), `MODEL_DIR` (model files), `LLAMA_SERVER`,
`PORT`/`SLOTS`/`CTX`/`NGL` (for `pdf2md serve`; defaults 8111/3/16384/99), `PDF2MD_PADDLE_SERVER`.

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
| server exits immediately / out of memory | read `/tmp/pdf2md-llama-server.log`; lower `SLOTS` or `CTX`; close other GPU programs |
| port already in use | set `PORT=…` for `pdf2md serve` and pass `--paddle-server http://127.0.0.1:PORT/v1` |
| Plugin: “could not run pdf2md” | Obsidian may not see `~/.local/bin`; set the full path in the plugin settings |

Run `pdf2md selftest` for an end-to-end test, and `pdf2md check` first: it reports the worker environment, model files, `llama-server`, the server and the GPU.
