#!/usr/bin/env bash
# Serve a workdir's dashboard page(s) over plain HTTP, so they're reachable
# from a normal browser at http://localhost:<port>.
#
# Standalone: runs a plain `python3 -m http.server`, not tied to any
# Claude Code session/tool -- start it whenever you want, from any
# terminal, and it keeps running until you Ctrl-C it (or close the
# terminal). Re-run this after regenerating a dashboard; it serves
# whatever's currently on disk, no restart needed for content changes,
# only if you want to serve a different directory or port.
#
#   ./scripts/serve_dashboard.sh /path/to/workdir [port]
#   ./scripts/serve_dashboard.sh /path/to/workdir            # defaults to port 8735
#
# Note: none of these pages need a server at all -- they embed their data
# inline, so opening one directly (double-click, or `open <file>.html`)
# works too. Use this script when you specifically want one reachable at a
# URL (sharing over a LAN, embedding, etc).

set -euo pipefail

DIR="${1:?Usage: serve_dashboard.sh <path/to/workdir> [port]}"
PORT="${2:-8735}"

# Lists whatever .html pages actually exist in $DIR -- there can be more
# than one (e.g. dashboard.html from build_comparison_dashboard.py and
# data_reuse_dashboard.html from build_data_reuse_dashboard.py both live in
# the same workdir), so this never hardcodes or guesses a single filename.
shopt -s nullglob
pages=("$DIR"/*.html)
shopt -u nullglob

if [[ ${#pages[@]} -eq 0 ]]; then
  echo "warning: no .html files found in $DIR -- run a build_*_dashboard.py script first" >&2
else
  echo "Serving $DIR at http://localhost:$PORT (Ctrl-C to stop). Pages found:"
  for p in "${pages[@]}"; do
    echo "  http://localhost:$PORT/$(basename "$p")"
  done
fi

exec python3 -m http.server "$PORT" --directory "$DIR"
