#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$SCRIPT_DIR/launcher-control.command" start
"$SCRIPT_DIR/launcher-control.command" open
