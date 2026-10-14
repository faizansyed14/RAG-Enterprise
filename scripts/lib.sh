#!/usr/bin/env bash
# Shared by scripts/{local,dev,prod}/*.sh. Source it; do not run it.
#
# Optional flags understood by every start script:
#   --models    also start the embedding + reranker model servers (compose profile "models"; BGE-M3 + bge-reranker-v2-m3)
#   --slim      build the images WITHOUT the local OCR / layout / table models and without LibreOffice (about 850 MB smaller;
#               scanned PDFs then go to the vision model and .doc / .xls / .ppt files are skipped)
# OCR, layout, tables and LibreOffice are built in by default.
# Pass the same flags to stop.sh so the optional services are stopped too.

PROFILE_ARGS=()

parse_flags() {
  REST_ARGS=()
  for a in "$@"; do
    case "$a" in
      --models)   PROFILE_ARGS+=(--profile models) ;;
      --slim)     export INSTALL_DOCPARSE=0 INSTALL_LEGACY_OFFICE=0 ;;
      *)          REST_ARGS+=("$a") ;;
    esac
  done
}

# Warn early when the local embedding model is configured but its files are missing.
check_local_models() {   # check_local_models <env file>
  local envfile="$1" backend dir
  backend="$(grep -m1 '^SCALE_EMBED_BACKEND=' "$envfile" | cut -d= -f2- | cut -d' ' -f1 || true)"
  dir="$(grep -m1 '^SCALE_ONNX_MODEL_DIR=' "$envfile" | cut -d= -f2- | cut -d' ' -f1 || true)"
  if [[ "$backend" == "onnx" && -n "$dir" ]]; then
    local host="./models/${dir#/models/}"
    if [[ ! -s "$host/onnx/model.onnx" || ! -s "$host/tokenizer.json" ]]; then
      echo "The local embedding model is not in $host yet. Run:  bash scripts/get-models.sh" >&2
      echo "(or switch SCALE_EMBED_BACKEND in $envfile to the model servers and start with --models)" >&2
      exit 1
    fi
  fi
}

# The reranker is optional (search works without it), so a missing one is a hint, not an error.
check_reranker() {   # check_reranker <env file>
  local envfile="$1" mode dir
  mode="$(grep -m1 '^SCALE_RERANK_MODE=' "$envfile" | cut -d= -f2- | cut -d' ' -f1 || true)"
  dir="$(grep -m1 '^SCALE_RERANK_ONNX_DIR=' "$envfile" | cut -d= -f2- | cut -d' ' -f1 || true)"
  if [[ "$mode" == "onnx" && -n "$dir" && ! -s "./models/${dir#/models/}/onnx/model.onnx" ]]; then
    echo "Note: the local reranker is not in ./models yet, so search will run without it. Fix:  bash scripts/get-models.sh" >&2
  fi
}

wait_for_postgres() {    # wait_for_postgres <compose command...>
  echo "Waiting for postgres..."
  for _ in $(seq 1 40); do
    if "$@" exec -T postgres pg_isready -U rag >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "postgres did not become ready" >&2
  return 1
}
