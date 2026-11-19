#!/bin/sh
# Auto-refresh docs/index.html whenever HTML files in docs/ are committed locally
REPO_ROOT="$(git rev-parse --show-toplevel)"
if [ -f "$REPO_ROOT/scripts/update_docs_index.py" ] && [ -f "$REPO_ROOT/docs/index.html" ]; then
  if command -v python3 >/dev/null 2>&1; then
    python3 "$REPO_ROOT/scripts/update_docs_index.py" --docs-dir "$REPO_ROOT/docs" >/dev/null 2>&1 && git add "$REPO_ROOT/docs/index.html"
  fi
fi
exit 0
