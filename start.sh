#!/bin/bash
if [[ -f "$(dirname "$0")/.env" ]]; then
  set -a
  source "$(dirname "$0")/.env"
  set +a
fi
exec python3 "$(dirname "$0")/proxy.py"
