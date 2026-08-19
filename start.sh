#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -f "$PROJECT_DIR/.env" ]; then
  echo "缺少 $PROJECT_DIR/.env，请先从 .env.example 复制并填写机器人凭据。" >&2
  exit 1
fi

set -a
. "$PROJECT_DIR/.env"
set +a

cd "$PROJECT_DIR"
exec "$PROJECT_DIR/.venv/bin/qq-guard-bot"
