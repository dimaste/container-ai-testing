#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_PATH="$ROOT_DIR/config/build_push.config.json"

if [[ $# -gt 0 && -f "$1" ]]; then
  CONFIG_PATH="$1"
  shift
fi

python3 "$ROOT_DIR/tools/build_push.py" --config "$CONFIG_PATH" "$@"
