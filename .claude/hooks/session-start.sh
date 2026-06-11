#!/bin/bash
# SessionStart-Hook für Claude Code on the web: Python-3.12-venv mit dem
# HA-Test-Stack (requirements-test.txt), damit pytest direkt läuft.
# Lokal (Mac) ist der Hook ein No-op.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

export PATH="$HOME/.local/bin:$PATH"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# HA-Anforderung: Python >= 3.12 (requirements-test.txt-Kopf)
if [ ! -d "$REPO_DIR/.venv" ]; then
  uv venv --python 3.12 "$REPO_DIR/.venv"
fi
uv pip install -q -r "$REPO_DIR/requirements-test.txt" --python "$REPO_DIR/.venv/bin/python"

echo "session-start: Tests via $REPO_DIR/.venv/bin/pytest"
