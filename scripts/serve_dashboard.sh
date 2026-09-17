#!/usr/bin/env bash
# Serve a workdir's dashboard.html (and everything else in it) over plain
# HTTP, so it's reachable from a normal browser at http://localhost:<port>.
#
# Standalone: runs a plain `python3 -m http.server`, not tied to any
# Claude Code session/tool -- start it whenever you want, from any
# terminal, and it keeps running until you Ctrl-C it (or close the
# terminal). Re-run this after regenerating dashboard.html; it serves
# whatever's currently on disk, no restart needed for content changes,
# only if you want to serve a different directory or port.
#
#   ./scripts/serve_dashboard.sh /path/to/workdir [port]
#   ./scripts/serve_dashboard.sh /path/to/workdir            # defaults to port 8735
#
# Note: dashboard.html itself needs no server at all -- it embeds its data
# inline, so opening it directly (double-click, or `open dashboard.html`)
# works too. Use this script when you specifically want it reachable at a
# URL (sharing over a LAN, embedding, etc).

set -euo pipefail

DIR="${1:?Usage: serve_dashboard.sh <path/to/workdir> [port]}"
PORT="${2:-8735}"

if [[ ! -f "$DIR/dashboard.html" ]]; then
  echo "warning: $DIR/dashboard.html not found -- run build_comparison_dashboard.py first" >&2
fi

echo "Serving $DIR at http://localhost:$PORT/dashboard.html (Ctrl-C to stop)"
exec python3 -m http.server "$PORT" --directory "$DIR"
