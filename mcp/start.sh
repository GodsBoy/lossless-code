#!/bin/bash
# lossless-code MCP server launcher
# Ensures the mcp Python package is available, then starts the server.
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Check if mcp package is available
if ! python3 -c "import mcp" 2>/dev/null; then
    pip3 install --break-system-packages --quiet mcp 2>/dev/null || \
    pip3 install --quiet mcp 2>/dev/null || \
    { echo "Error: 'mcp' Python package required. Install with: pip install mcp" >&2; exit 1; }
fi

exec python3 "$PLUGIN_ROOT/mcp/server.py"
