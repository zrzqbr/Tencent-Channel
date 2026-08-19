#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TENCENT_CLI_DIR=${TENCENT_CHANNEL_CLI_DIR:-"${HOME}/.local/bin"}
export PATH="$TENCENT_CLI_DIR:$PATH"

exec "$PROJECT_DIR/.venv/bin/qq-guard" \
  --config "$PROJECT_DIR/config.json" \
  tencent-monitor
