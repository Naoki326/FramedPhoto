#!/usr/bin/env bash
# test_sync_nas_filters.sh — 本地回归测试，不连接 NAS。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/sync_nas_filters.sh
source "$ROOT_DIR/scripts/sync_nas_filters.sh"

RSYNC_BIN="${RSYNC_BIN:-$(command -v rsync)}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "$TMP_DIR/src/照片" "$TMP_DIR/src/婚礼视频" "$TMP_DIR/src/N8BookData" "$TMP_DIR/dst"
printf 'image' > "$TMP_DIR/src/照片/upper.JPG"
printf 'image' > "$TMP_DIR/src/照片/lower.jpg"
printf 'image' > "$TMP_DIR/src/照片/upper.PNG"
printf 'image' > "$TMP_DIR/src/婚礼视频/wedding.JpG"
printf 'data' > "$TMP_DIR/src/N8BookData/hidden.JPG"
printf 'video' > "$TMP_DIR/src/clip.MP4"
printf 'text' > "$TMP_DIR/src/notes.txt"

"$RSYNC_BIN" -rltzn --itemize-changes --out-format='%n' \
  "${EXCLUDES[@]}" "$TMP_DIR/src/" "$TMP_DIR/dst/" > "$TMP_DIR/list"

assert_included() {
  grep -Fqx "$1" "$TMP_DIR/list" || {
    echo "FAIL: 应同步但未匹配: $1" >&2
    cat "$TMP_DIR/list" >&2
    exit 1
  }
}

assert_excluded() {
  if grep -Fqx "$1" "$TMP_DIR/list"; then
    echo "FAIL: 应排除但被匹配: $1" >&2
    cat "$TMP_DIR/list" >&2
    exit 1
  fi
}

assert_included '照片/upper.JPG'
assert_included '照片/lower.jpg'
assert_included '照片/upper.PNG'
assert_included '婚礼视频/wedding.JpG'
assert_excluded 'N8BookData/hidden.JPG'
assert_excluded 'clip.MP4'
assert_excluded 'notes.txt'

echo 'PASS: NAS 图片白名单覆盖大小写扩展名，并排除非图片数据'
