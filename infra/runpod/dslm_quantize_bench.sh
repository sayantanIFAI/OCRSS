#!/usr/bin/env bash
# Quantize the fine-tuned DSLM to AWQ/INT4 and benchmark it against the bf16 7B (backlog E2-S6).
# Runbook: docs/quantization.md. Needs a GPU host already bootstrapped (bootstrap_pod.sh built
# .venv); builds .venv-quant and .venv-vllm from their hash-checked locks if they are missing.
#
#   CHECKPOINT=/workspace/models/cdi-dslm  bash infra/runpod/dslm_quantize_bench.sh [documents...]
#
# CHECKPOINT  the bf16 checkpoint to quantize (E2-S5's merged output)          (required)
# BASELINE    the bf16 reference the block compares against                    (Qwen2.5-VL-7B)
# DATA_CLASS  synthetic | deidentified - nothing else may enter the corpus      (synthetic)
# LABELS      space-separated <file>=<doc_type> for documents the heuristic is unsure of
# documents   default: data/samples/*
set -euo pipefail
WS=${WS:-/workspace}
REPO=${REPO:-$WS/cdi}
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/versions.env"
set -a; [ -f "$REPO/.env" ] && . "$REPO/.env"; set +a
export HF_HOME=${HF_HOME:-$WS/hf-cache}

: "${CHECKPOINT:?set CHECKPOINT to the bf16 checkpoint to quantize (E2-S5)}"
BASELINE=${BASELINE:-Qwen/Qwen2.5-VL-7B-Instruct}
DATA_CLASS=${DATA_CLASS:-synthetic}
RUN=${RUN:-$WS/dslm/run-$(date -u +%Y%m%dT%H%M%SZ)}
QUANT_OUT=${QUANT_OUT:-$WS/models/$(basename "$CHECKPOINT")-awq}
ARCHIVE=${ARCHIVE:-$REPO/docs/bench/e2-s6}
if [ "$#" -eq 0 ]; then set -- "$REPO"/data/samples/*; fi
mkdir -p "$RUN"

venv() {  # venv <dir> <lock>: the locked set, hash-checked, torch inherited or the pinned cu128
  [ -x "$1/bin/python" ] || python3 -m venv --system-site-packages "$1"
  "$1/bin/python" -m pip install -q --require-hashes --no-deps -r "$2"
  "$1/bin/python" -m pip install -q --no-deps --no-build-isolation -e "$REPO"
  if ! "$1/bin/python" -c "import torch, torchvision, torchaudio" 2>/dev/null; then
    "$1/bin/python" -m pip install -q --no-deps "torch==$TORCH_VERSION" \
      "torchvision==$TORCHVISION_VERSION" "torchaudio==$TORCHAUDIO_VERSION" \
      --index-url https://download.pytorch.org/whl/cu128
  fi
}
venv "$REPO/.venv-quant" "$HERE/requirements-quant.lock"
venv "$REPO/.venv-vllm" "$HERE/requirements-vllm.lock"

cd "$REPO"
label_args=()
for l in ${LABELS:-}; do label_args+=(--label "$l"); done
echo "== 1/4 corpus ($DATA_CLASS, $# documents)"
"$REPO/.venv/bin/python" -m cdi_adapter.dslm.corpus build --out "$RUN/corpus" \
  --data-class "$DATA_CLASS" "${label_args[@]}" "$@"

echo "== 2/4 quantize $CHECKPOINT -> $QUANT_OUT"
"$REPO/.venv-quant/bin/python" -m cdi_adapter.dslm.quantize --checkpoint "$CHECKPOINT" \
  --corpus "$RUN/corpus" --out "$QUANT_OUT" 2>&1 | tee "$RUN/quantize.log"

echo "== 3/4 benchmark (one process per variant, so the GPU is freed between them)"
bench() {
  "$REPO/.venv-vllm/bin/python" -m cdi_adapter.dslm.bench run --name "$1" --model "$2" \
    --corpus "$RUN/corpus" --out "$RUN/$1.json" 2>&1 | tee "$RUN/$1.log"
}
bench baseline-bf16 "$BASELINE"
results=("$RUN/baseline-bf16.json")
if [ "$CHECKPOINT" != "$BASELINE" ]; then
  bench dslm-bf16 "$CHECKPOINT"; results+=("$RUN/dslm-bf16.json")
fi
bench dslm-awq "$QUANT_OUT"; results+=("$RUN/dslm-awq.json")

echo "== 4/4 compare -> $ARCHIVE"
"$REPO/.venv/bin/python" -m cdi_adapter.dslm.bench compare "${results[@]}" --archive "$ARCHIVE"
