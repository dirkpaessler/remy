#!/bin/sh
# Start Remy's MCP server, finding uv ourselves.
#
# The desktop app launches MCP servers with a minimal PATH (no ~/.local/bin,
# no /opt/homebrew/bin), so a bare "uv" command silently never starts and
# Remy simply does not appear in Cowork. /bin/sh always exists; this script
# looks in the places uv actually lives and execs it, keeping stdio intact.

HERE="$(cd "$(dirname "$0")" && pwd)"

for UV in uv "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
  if command -v "$UV" >/dev/null 2>&1; then
    exec "$UV" run --script "$HERE/remy_mcp.py"
  fi
done

echo "remy: uv was not found; install it with:" >&2
echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
exit 1
