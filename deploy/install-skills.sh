#!/usr/bin/env bash
# deploy/install-skills.sh — sync repo skills/ to ~/.hermes/skills/ (v2.2)
#
# Usage: ./deploy/install-skills.sh [HERMES_HOME]
# Default HERMES_HOME = ~/.hermes
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
SOURCE="$REPO_ROOT/skills"
HERMES_HOME="${1:-$HOME/.hermes}"
DEST="$HERMES_HOME/skills"

if [ ! -d "$SOURCE" ]; then
    echo "[install-skills] ERROR: source dir not found: $SOURCE"
    exit 1
fi

mkdir -p "$DEST"

for skill_dir in "$SOURCE"/*/; do
    skill_name="$(basename "$skill_dir")"
    src_md="$skill_dir/SKILL.md"
    if [ ! -f "$src_md" ]; then
        echo "[install-skills] SKIP $skill_name (no SKILL.md)"
        continue
    fi
    dst_dir="$DEST/$skill_name"
    mkdir -p "$dst_dir"
    # Backup existing if different
    if [ -f "$dst_dir/SKILL.md" ]; then
        if ! diff -q "$src_md" "$dst_dir/SKILL.md" > /dev/null 2>&1; then
            cp "$dst_dir/SKILL.md" "$dst_dir/SKILL.md.bak"
            echo "[install-skills] UPDATED $skill_name (backup: SKILL.md.bak)"
        else
            echo "[install-skills] OK $skill_name (already up to date)"
        fi
    else
        echo "[install-skills] INSTALL $skill_name"
    fi
    # Copy SKILL.md and any resources/
    cp "$src_md" "$dst_dir/SKILL.md"
    if [ -d "$skill_dir/resources" ]; then
        cp -r "$skill_dir/resources" "$dst_dir/"
    fi
    if [ -d "$skill_dir/templates" ]; then
        cp -r "$skill_dir/templates" "$dst_dir/"
    fi
    if [ -d "$skill_dir/scripts" ]; then
        cp -r "$skill_dir/scripts" "$dst_dir/"
    fi
done

echo "[install-skills] Done. Skills synced to $DEST"
