#!/bin/sh
set -e
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cp "$REPO_ROOT/scripts/pre-commit-hook.sh" "$REPO_ROOT/.git/hooks/pre-commit"
chmod +x "$REPO_ROOT/.git/hooks/pre-commit"
echo "[OK] Installed local pre-commit auto-refresh hook in $REPO_ROOT/.git/hooks/pre-commit"
