#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${RUN_DIR:-$ROOT/swebench_runs/lite_20}"
DJANGO_TASKS_DIR="${DJANGO_TASKS_DIR:-$ROOT/swebench_runs/lite_20_django/tasks}"
PREDICTIONS_PATH="${PREDICTIONS_PATH:-$RUN_DIR/predictions.patchflow.jsonl}"
INSTANCE_IDS_PATH="${INSTANCE_IDS_PATH:-$RUN_DIR/instance_ids.txt}"
INSTANCES_JSONL="${INSTANCES_JSONL:-$RUN_DIR/instances.jsonl}"
ARTIFACTS_DIR="${ARTIFACTS_DIR:-$ROOT/logs/artifacts}"
MODEL_KEY="${SILICONFLOW_API_KEY:-}"

usage() {
  cat <<'EOF'
Usage:
  scripts/run_lite20_low_bandwidth.sh prepare-local
  scripts/run_lite20_low_bandwidth.sh run-one <task_file_name>
  scripts/run_lite20_low_bandwidth.sh run-seq <task_file_name> [<task_file_name> ...]
  scripts/run_lite20_low_bandwidth.sh collect
  scripts/run_lite20_low_bandwidth.sh official <run_id>

Notes:
  - All modes reuse local instances/tasks/repos/artifacts and avoid dataset/repo redownloads.
  - `official` still uses Docker and may consume network if required images are not already cached.
EOF
}

require_file() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    echo "Missing file: $path" >&2
    exit 1
  fi
}

require_dir() {
  local path="$1"
  if [[ ! -d "$path" ]]; then
    echo "Missing directory: $path" >&2
    exit 1
  fi
}

prepare_local() {
  require_file "$INSTANCES_JSONL"
  python "$ROOT/scripts/swebench_lite_adapter.py" prepare \
    --limit 20 \
    --output-dir "$RUN_DIR" \
    --instances-jsonl "$INSTANCES_JSONL"
}

run_one() {
  local task_name="$1"
  if [[ -z "$MODEL_KEY" ]]; then
    echo "SILICONFLOW_API_KEY is not set." >&2
    exit 1
  fi
  require_dir "$DJANGO_TASKS_DIR"
  /bin/zsh -lc "cd '$ROOT' && SILICONFLOW_API_KEY='$MODEL_KEY' conda run -n patchflow-swebench python -m entry.cli benchmark run --repo '$ROOT' --tasks-dir '$DJANGO_TASKS_DIR' --task-glob '$task_name' --limit 1 --mechanism-profile full --task-timeout-seconds 1800 --stream"
}

run_seq() {
  shift
  for task_name in "$@"; do
    echo "=== RUN $task_name ==="
    run_one "$task_name"
    echo "=== DONE $task_name ==="
  done
}

collect_predictions() {
  require_file "$INSTANCES_JSONL"
  python "$ROOT/scripts/swebench_lite_adapter.py" collect-predictions \
    --instances-jsonl "$INSTANCES_JSONL" \
    --artifacts-dir "$ARTIFACTS_DIR" \
    --output "$PREDICTIONS_PATH" \
    --model-name patchflow-deepseek-v4-flash
}

run_official() {
  local run_id="$1"
  require_file "$PREDICTIONS_PATH"
  require_file "$INSTANCE_IDS_PATH"
  bash "$ROOT/scripts/run_swebench_official_eval.sh" \
    "$PREDICTIONS_PATH" \
    "$run_id" \
    "$INSTANCE_IDS_PATH"
}

cmd="${1:-}"
case "$cmd" in
  prepare-local)
    prepare_local
    ;;
  run-one)
    shift
    if [[ $# -ne 1 ]]; then
      usage
      exit 1
    fi
    run_one "$1"
    ;;
  run-seq)
    if [[ $# -lt 2 ]]; then
      usage
      exit 1
    fi
    run_seq "$@"
    ;;
  collect)
    collect_predictions
    ;;
  official)
    shift
    if [[ $# -ne 1 ]]; then
      usage
      exit 1
    fi
    run_official "$1"
    ;;
  *)
    usage
    exit 1
    ;;
esac
