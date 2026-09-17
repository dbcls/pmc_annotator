#!/usr/bin/env bash
# Publish a run's dashboard.html to docs/index.html for GitHub Pages.
#
# Manual, single-latest-page publishing: this overwrites docs/index.html
# with the given dashboard and creates a commit -- it does NOT push. Review
# the diff and push yourself when ready:
#
#   ./scripts/publish_dashboard.sh /path/to/workdir/dashboard.html "run_17th_September"
#   git push origin <branch>
#
# One-time GitHub Pages setup (repo Settings -> Pages): set source to
# "Deploy from a branch", branch = the branch you push docs/ on, folder = /docs.

set -euo pipefail

DASHBOARD_SRC="${1:?Usage: publish_dashboard.sh <path/to/dashboard.html> [label]}"
LABEL="${2:-$(date +%Y-%m-%d)}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCS_DIR="$REPO_ROOT/docs"

if [[ ! -f "$DASHBOARD_SRC" ]]; then
  echo "error: $DASHBOARD_SRC does not exist" >&2
  exit 1
fi

mkdir -p "$DOCS_DIR"
cp "$DASHBOARD_SRC" "$DOCS_DIR/index.html"

cd "$REPO_ROOT"
git add docs/index.html
if git diff --cached --quiet; then
  echo "docs/index.html is unchanged (identical to $DASHBOARD_SRC) -- nothing to commit."
  exit 0
fi

git commit -m "Publish comparison dashboard: ${LABEL}"
echo
echo "Committed. Review with 'git show --stat HEAD', then push yourself:"
echo "  git push origin $(git rev-parse --abbrev-ref HEAD)"
