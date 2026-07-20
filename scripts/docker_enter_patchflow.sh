#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-patchflow-dev}"
WORKSPACE_DIR="${WORKSPACE_DIR:-$PWD}"

docker build \
  -f "$WORKSPACE_DIR/docker/patchflow-dev.Dockerfile" \
  -t "$IMAGE_NAME" \
  "$WORKSPACE_DIR"

docker run --rm -it \
  -e SILICONFLOW_API_KEY \
  -e OPENAI_API_KEY \
  -e ANTHROPIC_API_KEY \
  -v "$WORKSPACE_DIR:/workspace/PatchFlow" \
  -w /workspace/PatchFlow \
  "$IMAGE_NAME" \
  bash
