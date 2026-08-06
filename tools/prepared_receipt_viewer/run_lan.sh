#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${PROJECT_ROOT}/.venv/bin/python}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8092}"

exec "${PYTHON_BIN}" -m uvicorn \
  tools.prepared_receipt_viewer.app:app \
  --host "${HOST}" \
  --port "${PORT}"