#!/usr/bin/env bash
# Publish a built dashboard HTML file to the gh-pages branch of this repo,
# so it's served at https://dbcls.github.io/pmc_annotator/ via GitHub Pages.
#
# Usage:
#   ./scripts/deploy_dashboard_to_pages.sh <path-to-dashboard.html> ["commit message"]
#
# Example:
#   ./scripts/deploy_dashboard_to_pages.sh \
#     "/Users/vsubramoniam/Code/Identifiers-Hackathon/output/run_17th_September/data_reuse_dashboard.html"
#
# What it does:
#   1. Creates a temporary git worktree checked out to the gh-pages branch
#      (creating the branch if it doesn't exist yet).
#   2. Copies the given HTML file in as index.html.
#   3. Commits and pushes to origin/gh-pages.
#   4. Cleans up the temporary worktree.
# Your current branch/checkout is never touched.

set -euo pipefail

SRC_HTML="${1:-}"
COMMIT_MSG="${2:-Update dashboard snapshot}"

if [[ -z "$SRC_HTML" ]]; then
  echo "usage: $0 <path-to-dashboard.html> [\"commit message\"]" >&2
  exit 1
fi
if [[ ! -f "$SRC_HTML" ]]; then
  echo "error: file not found: $SRC_HTML" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "error: your current checkout has uncommitted changes to tracked files. Commit or stash them first." >&2
  exit 1
fi

WORKTREE_DIR="$(mktemp -d /tmp/pmc_gh_pages_worktree.XXXXXX)"
cleanup() { git worktree remove "$WORKTREE_DIR" --force >/dev/null 2>&1 || true; }
trap cleanup EXIT

if git show-ref --verify --quiet refs/remotes/origin/gh-pages; then
  echo "Checking out existing gh-pages branch..."
  git worktree add "$WORKTREE_DIR" gh-pages
elif git show-ref --verify --quiet refs/heads/gh-pages; then
  git worktree add "$WORKTREE_DIR" gh-pages
else
  echo "No gh-pages branch found yet -- creating it as an orphan branch..."
  git worktree add "$WORKTREE_DIR" --detach
  (cd "$WORKTREE_DIR" && git checkout --orphan gh-pages && git rm -rf . >/dev/null 2>&1 || true)
fi

cp "$SRC_HTML" "$WORKTREE_DIR/index.html"

pushd "$WORKTREE_DIR" >/dev/null
git add index.html
if git diff --cached --quiet; then
  echo "No changes -- index.html is identical to what's already published."
else
  git commit -m "$COMMIT_MSG"
  git push -u origin gh-pages
  echo "Pushed. Live in a minute or two at your GitHub Pages URL."
fi
popd >/dev/null
