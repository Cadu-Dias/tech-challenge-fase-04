#!/bin/sh
set -eu
exec python3 "$(dirname "$0")/generate-auth-500.py" \
  --run --allow-external-notifications --duration 300 --interval 0.5 "$@"
