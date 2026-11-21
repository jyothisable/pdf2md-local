# Installing pdf2md-local

Everything that installs or downloads is done by **you, in a terminal**. The Obsidian plugin only checks and uses it.

## Linux (tested)

1. **uv** — <https://docs.astral.sh/uv/getting-started/installation/>
2. **llama-server** from [llama.cpp](https://github.com/ggml-org/llama.cpp), built with your GPU backend, on `PATH`
   (or set `LLAMA_SERVER=/path/to/llama-server`):
   ```bash
   git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp
   cmake -B build -DGGML_CUDA=ON && cmake --build build -j --target llama-server
   export PATH="$PWD/build/bin:$PATH"
   ```
3. The tool and its backend:
   ```bash
   uv tool install git+https://github.com/jyothisable/pdf2md-local
   pdf2md setup     # creates ~/.local/share/pdf2md-local/worker-venv and downloads ~1 GB of model files
   pdf2md check
   ```
4. Convert: `pdf2md serve` in one terminal, `pdf2md book.pdf --split-chapters` in another.

`pdf2md setup` is safe to repeat; an interrupted model download resumes.

Environment variables: `PDF2MD_HOME` (worker environment), `MODEL_DIR` (model files), `LLAMA_SERVER`,
`PORT`/`SLOTS`/`CTX`/`NGL` (for `pdf2md serve`; defaults 8111/3/16384/99), `PDF2MD_PADDLE_SERVER`.

## macOS and Windows (untested)

llama.cpp supports Metal and Vulkan/CUDA builds, and the Python dependencies publish wheels for those systems,
so it may work, but **nobody has confirmed it**. Please report what happens in an issue.

## Troubleshooting

| Message | What to do |
|---|---|
| `missing worker environment` / `missing model files` | run `pdf2md setup` |
| `llama-server not found` | install llama.cpp and put `llama-server` on `PATH`, or set `LLAMA_SERVER` |
| `no PaddleOCR-VL server at …` | run `pdf2md serve`, or add `--serve-command "pdf2md serve"` |
| server exits immediately / out of memory | read `/tmp/pdf2md-llama-server.log`; lower `SLOTS` or `CTX`; close other GPU programs |
| port already in use | set `PORT=…` for `pdf2md serve` and pass `--paddle-server http://127.0.0.1:PORT/v1` |
| Plugin: “could not run pdf2md” | Obsidian may not see `~/.local/bin`; set the full path in the plugin settings |

Run `pdf2md check` first: it reports the worker environment, model files, `llama-server`, the server and the GPU.
