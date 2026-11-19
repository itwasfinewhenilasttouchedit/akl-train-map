#!/usr/bin/env bash
# Start the Auckland live train map.
#   ./run.sh            -> http://localhost:8731
#   PORT=9000 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  uv venv .venv
  uv pip install --python .venv/bin/python fastapi httpx "uvicorn[standard]"
fi

# Rebuild static rail geometry if missing or older than 7 days.
if [ ! -f output/rail_geo.json ] || [ -n "$(find output/rail_geo.json -mtime +7 2>/dev/null)" ]; then
  echo "building rail geometry from AT GTFS feed..."
  rm -f output/gtfs.zip
  .venv/bin/python src/build_static.py
fi

exec .venv/bin/python src/server.py
