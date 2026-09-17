#!/bin/bash
# launchd ExecStart wrapper for the Talos executor.
# Installed by install-launchd.sh to $TALOS_HOME/run-executor.sh.
# Sources talos.env + .env, sets PATH for Docker, and runs unbuffered Python.

set -euo pipefail

# Source environment files
if [ -f "$HOME/.hermes/talos.env" ]; then
    set -a; source "$HOME/.hermes/talos.env"; set +a
fi
if [ -f "$HOME/.hermes/.env" ]; then
    set -a; source "$HOME/.hermes/.env"; set +a
fi

# PATH must include Docker (symlink in /usr/local/bin, real binary in Docker.app)
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/Docker.app/Contents/Resources/bin

# Resolve repo + hermes-agent paths
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HERMES_AGENT="${HERMES_HOME:-$HOME/.hermes}/hermes-agent"
export PYTHONPATH="${REPO_DIR}:${HERMES_AGENT}"

# Unbuffered stdout/stderr so self-check output is visible in launchd logs
export PYTHONUNBUFFERED=1

cd "$REPO_DIR"
exec /usr/local/bin/python3 -u -m talos.executor.main
