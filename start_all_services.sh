#!/usr/bin/env bash
# Compatibility launcher; the implementation lives under deploy/.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/deploy/start_all_services.sh" "$@"
