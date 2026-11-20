#!/usr/bin/env bash
# Download PaddleOCR-VL-1.6 (GGUF, Apache-2.0) and serve it with llama.cpp's llama-server. No Ollama needed.
#
#   serve-paddleocr-vl.sh            # download if missing, then run the server in the foreground
#   serve-paddleocr-vl.sh --download # only download
#
# Needs `llama-server` (build llama.cpp with CUDA/Metal/Vulkan: https://github.com/ggml-org/llama.cpp) on PATH, or
# LLAMA_SERVER=/path/to/llama-server. Then convert with:
#   pdf2md book.pdf --engine paddleocr-vl --paddle-server http://127.0.0.1:8111/v1
# or let pdf2md start and stop the server itself:
#   pdf2md book.pdf --engine paddleocr-vl --serve-command "$(realpath serve-paddleocr-vl.sh)"
#
# Environment: PORT (8111), MODEL_DIR (~/.cache/pdf2md/models/paddleocr-vl-1.6), SLOTS (3 parallel requests),
#              CTX (16384 in total, split across the slots), NGL (99 = all layers on the GPU).
set -euo pipefail

REPO="PaddlePaddle/PaddleOCR-VL-1.6-GGUF"
MODEL_DIR="${MODEL_DIR:-$HOME/.cache/pdf2md/models/paddleocr-vl-1.6}"
PORT="${PORT:-8111}"
SLOTS="${SLOTS:-3}"
CTX="${CTX:-16384}"
NGL="${NGL:-99}"
FILES=("PaddleOCR-VL-1.6-GGUF.gguf" "PaddleOCR-VL-1.6-GGUF-mmproj.gguf")

mkdir -p "$MODEL_DIR"
for f in "${FILES[@]}"; do
	if [ ! -s "$MODEL_DIR/$f" ]; then
		echo "downloading $f (about 1 GB in total) ..." >&2
		curl -fL --retry 3 -C - -o "$MODEL_DIR/$f.part" "https://huggingface.co/$REPO/resolve/main/$f"
		mv "$MODEL_DIR/$f.part" "$MODEL_DIR/$f"
	fi
done
[ "${1:-}" = "--download" ] && { echo "models are in $MODEL_DIR" >&2; exit 0; }

SERVER="${LLAMA_SERVER:-llama-server}"
command -v "$SERVER" >/dev/null 2>&1 || [ -x "$SERVER" ] || {
	echo "error: llama-server not found. Build llama.cpp, then put llama-server on PATH or set LLAMA_SERVER." >&2
	exit 1
}
exec "$SERVER" -m "$MODEL_DIR/${FILES[0]}" --mmproj "$MODEL_DIR/${FILES[1]}" \
	--host 127.0.0.1 --port "$PORT" --temp 0 -ngl "$NGL" -c "$CTX" -np "$SLOTS" --alias paddleocr-vl
