#!/bin/bash
# 单向把记忆目录推到另一台机器。所有值用环境变量覆盖。
#
#   DEST_HOST=user@host SRC_MEM=/path/memory ./push-memory.sh
#
set -euo pipefail

DEST="${DEST_HOST:?set DEST_HOST, e.g. user@host}"
KEY="${DEST_KEY:-$HOME/.ssh/id_ed25519}"
SRC_MEM="${SRC_MEM:?set SRC_MEM (directory containing *.md)}"
SRC_DIARY="${SRC_DIARY:-}"
REMOTE_BIN="${REMOTE_BIN:-pull-memory.sh}"
LOG="${LOG:-./push-memory.log}"

STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/memory" "$STAGE/diary-from-main"

cp -r "$SRC_MEM/." "$STAGE/memory/"

if [ -n "$SRC_DIARY" ] && [ -d "$SRC_DIARY" ]; then
  for f in "$SRC_DIARY"/*.md; do
    [ -f "$f" ] || continue
    {
      printf '> **Author: the machine that pushed this file, not the receiver.**\n\n---\n\n'
      cat "$f"
    } > "$STAGE/diary-from-main/$(basename "$f")"
  done
fi

tar czf "$STAGE/bundle.tgz" -C "$STAGE" memory diary-from-main

if ! scp -q -i "$KEY" -o StrictHostKeyChecking=accept-new \
     "$STAGE/bundle.tgz" "$DEST:/tmp/mem-in.tgz"; then
  echo "$(date '+%F %T') push failed: scp" >> "$LOG"
  exit 1
fi

ssh -i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
    "$DEST" "bash ~/bin/$REMOTE_BIN" || true

echo "$(date '+%F %T') pushed $(du -h "$STAGE/bundle.tgz" | cut -f1)" >> "$LOG"
