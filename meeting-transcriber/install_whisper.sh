#!/usr/bin/env bash
# Install the local Whisper floor: the whisper.cpp binary + one GGML model.
# Usage: ./install_whisper.sh [model]
#   model defaults to large-v3-turbo-q5_0 (~550 MB, near-best quality, ~8x faster).
#   Other options: large-v3-turbo, large-v3, medium, small, base, tiny
#   (and their -q5_0 / -q8_0 quantized variants).
set -euo pipefail

MODEL="${1:-large-v3-turbo-q5_0}"
MODELS_DIR="$HOME/.meeting-transcriber/models"
URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${MODEL}.bin"
DEST="$MODELS_DIR/ggml-${MODEL}.bin"

if ! command -v whisper-cli >/dev/null 2>&1 \
   && [ ! -x /opt/homebrew/bin/whisper-cli ] \
   && [ ! -x /opt/homebrew/bin/whisper-cpp ]; then
  if command -v brew >/dev/null 2>&1; then
    echo "Installing whisper-cpp via Homebrew..."
    brew install whisper-cpp
  else
    echo "Homebrew not found. Install it from https://brew.sh, then: brew install whisper-cpp" >&2
    exit 1
  fi
fi

mkdir -p "$MODELS_DIR"
if [ -f "$DEST" ]; then
  echo "Model already present: $DEST"
else
  echo "Downloading $URL"
  curl -L --fail -o "$DEST.download" "$URL"
  mv "$DEST.download" "$DEST"
fi

echo "Whisper ready:"
echo "  binary: $(command -v whisper-cli || echo /opt/homebrew/bin/whisper-cli)"
echo "  model:  $DEST"
echo "Set \"transcribe_provider\": \"local_whisper\" in config.json to use it."
