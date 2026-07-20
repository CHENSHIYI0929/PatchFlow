#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <predictions_jsonl> <run_id> [instance_ids_file]" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SWEBENCH_ROOT="$ROOT/SWE-bench"
PREDICTIONS_PATH="$1"
RUN_ID="$2"
INSTANCE_IDS_FILE="${3:-}"

export SWEBENCH_DOCKER_HTTP_PROXY="${SWEBENCH_DOCKER_HTTP_PROXY:-http://host.docker.internal:7897}"
export SWEBENCH_DOCKER_HTTPS_PROXY="${SWEBENCH_DOCKER_HTTPS_PROXY:-$SWEBENCH_DOCKER_HTTP_PROXY}"
export SWEBENCH_DOCKER_ALL_PROXY="${SWEBENCH_DOCKER_ALL_PROXY:-$SWEBENCH_DOCKER_HTTP_PROXY}"
export SWEBENCH_DOCKER_NO_PROXY="${SWEBENCH_DOCKER_NO_PROXY:-localhost,127.0.0.1,host.docker.internal}"

if [[ ! -f "$PREDICTIONS_PATH" ]]; then
  echo "Predictions file not found: $PREDICTIONS_PATH" >&2
  exit 1
fi

ARGS=(
  --dataset_name princeton-nlp/SWE-bench_Lite
  --split test
  --predictions_path "$PREDICTIONS_PATH"
  --max_workers 1
  --run_id "$RUN_ID"
  --namespace ""
)

if [[ -n "$INSTANCE_IDS_FILE" ]]; then
  if [[ ! -f "$INSTANCE_IDS_FILE" ]]; then
    echo "Instance IDs file not found: $INSTANCE_IDS_FILE" >&2
    exit 1
  fi

  INSTANCE_IDS=()
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" ]] && continue
    INSTANCE_IDS+=("$line")
  done < "$INSTANCE_IDS_FILE"
  if [[ ${#INSTANCE_IDS[@]} -gt 0 ]]; then
    ARGS+=(--instance_ids "${INSTANCE_IDS[@]}")
  fi
fi

cd "$SWEBENCH_ROOT"
python -m swebench.harness.run_evaluation "${ARGS[@]}"
