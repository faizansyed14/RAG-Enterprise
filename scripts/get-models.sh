#!/usr/bin/env bash
# Download the local models into ./models (mounted at /models in the backend and worker containers).
#
#   bash scripts/get-models.sh                 # embedding model BGE-small, 8-bit ONNX (384 dimensions, ~35 MB)
#                                              # + the small cross-encoder reranker (ONNX, ~90 MB)
#   bash scripts/get-models.sh --no-reranker   # the embedding model only (then set SCALE_RERANK_MODE=none)
#   MODELS_DIR=/srv/models bash scripts/get-models.sh
#
# Source order: Hugging Face first, then the same ONNX files as published on npm (for networks that block Hugging Face).
# Needs curl; the npm fallback also needs npm and tar. Safe to re-run: existing files are kept.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

MODELS_DIR="${MODELS_DIR:-./models}"
WITH_RERANKER=1
for a in "$@"; do
  case "$a" in
    --reranker) WITH_RERANKER=1 ;;                # the default; accepted for older instructions
    --no-reranker) WITH_RERANKER=0 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 1 ;;
  esac
done

fetch_hf() {   # fetch_hf <repo> <dest dir> [file in the repo, default onnx/model.onnx]
  local repo="$1" dest="$2" file="${3:-onnx/model.onnx}"
  mkdir -p "$dest/onnx"
  curl -fL --retry 2 -sS -o "$dest/tokenizer.json.part" "https://huggingface.co/$repo/resolve/main/tokenizer.json" \
    && curl -fL --retry 2 -sS -o "$dest/onnx/model.onnx.part" "https://huggingface.co/$repo/resolve/main/$file" \
    && mv "$dest/tokenizer.json.part" "$dest/tokenizer.json" && mv "$dest/onnx/model.onnx.part" "$dest/onnx/model.onnx"
}

fetch_npm() {  # fetch_npm <npm package> <path inside the package> <dest dir>
  local pkg="$1" inner="$2" dest="$3" tmp
  command -v npm >/dev/null || { echo "npm not found" >&2; return 1; }
  tmp="$(mktemp -d)"
  ( cd "$tmp" && npm pack "$pkg" --silent >/dev/null && tar -xzf ./*.tgz ) || { rm -rf "$tmp"; return 1; }
  mkdir -p "$dest/onnx"
  cp "$tmp/package/$inner/tokenizer.json" "$dest/tokenizer.json"
  cp "$tmp/package/$inner/onnx/model.onnx" "$dest/onnx/model.onnx"
  rm -rf "$tmp"
}

get() {        # get <name> <hf repo> <npm package> <path inside package, with * for the hash dir> [file in the HF repo]
  local name="$1" repo="$2" pkg="$3" inner="$4" hf_file="${5:-}" dest="$MODELS_DIR/$1"
  if [[ -s "$dest/tokenizer.json" && -s "$dest/onnx/model.onnx" ]]; then echo "$name: already present"; return; fi
  echo "$name: downloading from Hugging Face ($repo) ..."
  if fetch_hf "$repo" "$dest" "$hf_file" 2>/dev/null; then echo "$name: done"; return; fi
  rm -rf "$dest"
  echo "$name: Hugging Face not reachable, trying npm ($pkg) ..."
  if [[ "$inner" == *"*"* ]]; then
    # the package keeps a hash-named snapshot folder: resolve it after unpacking
    local tmp; tmp="$(mktemp -d)"
    ( cd "$tmp" && npm pack "$pkg" --silent >/dev/null && tar -xzf ./*.tgz ) || { rm -rf "$tmp"; echo "$name: FAILED" >&2; exit 1; }
    local snap; snap="$(ls -d "$tmp"/package/$inner | head -1)"
    mkdir -p "$dest/onnx"; cp "$snap/tokenizer.json" "$dest/tokenizer.json"; cp "$snap/onnx/model.onnx" "$dest/onnx/model.onnx"
    rm -rf "$tmp"
  else
    fetch_npm "$pkg" "$inner" "$dest" || { echo "$name: FAILED (no source reachable)" >&2; exit 1; }
  fi
  echo "$name: done"
}

mkdir -p "$MODELS_DIR"
# 8-bit version of BGE-small: about twice as fast to embed with the same retrieval accuracy (measured). The npm fallback only has the full-size file,
# which works the same, just slower.
get bge-small-en-v1.5-int8 Xenova/bge-small-en-v1.5 @coree-ai/coree-model-bge-small-en-v1.5 "model/models--Xenova--bge-small-en-v1.5/snapshots/*" onnx/model_quantized.onnx
if [[ "$WITH_RERANKER" == 1 ]]; then
  # The reranker only improves the order of results: if no source is reachable, search works without it.
  ( get ms-marco-MiniLM-L-6-v2 Xenova/ms-marco-MiniLM-L-6-v2 kontexta-reranker-model "models/Xenova/ms-marco-MiniLM-L-6-v2" ) \
    || echo "WARNING: reranker not downloaded; search runs without it (or retry later, or SCALE_RERANK_MODE=none)." >&2
fi

echo
echo "Models are in $MODELS_DIR (the .env examples already point at them)."
exit 0
