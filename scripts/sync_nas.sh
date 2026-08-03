#!/usr/bin/env bash
# sync_nas.sh — 从远程群晖 NAS 增量同步照片到本机（rsync over SSH，IPv6 友好）
#
# 适用场景：NAS 在远程（IPv6 公网 / 公网 IP），相比 SMB 直连：
#   - SSH 加密，不暴露 SMB 445 端口
#   - 增量同步：只传新增/变化的照片，日常流量极小
#   - 断线自动重试，适合公网链路
#
# 用法：
#   ./scripts/sync_nas.sh            同步（增量）
#   ./scripts/sync_nas.sh --dry-run  演练（不实际传输）
#   ./scripts/sync_nas.sh --delete   同步并删除本机已不在 NAS 的照片
#
# 配置（services/.env）：
#   NAS_SSH_HOST=fe80::... 或 240e:...    NAS 的 IPv6/域名
#   NAS_SSH_USER=username                  SSH 用户（群晖管理员）
#   NAS_SSH_PORT=22
#   NAS_PHOTO_DIR=/volume1/photo           远程照片目录
#   PHOTO_LIB_DIR=./photos                 本机同步目录（默认 services/photos）
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE="services/.env"
if [ -f "$ENV_FILE" ]; then
  set -a; source "$ENV_FILE"; set +a
fi

: "${NAS_SSH_HOST:?需要 NAS_SSH_HOST（IPv6 地址或域名）}"
: "${NAS_SSH_USER:?需要 NAS_SSH_USER}"
NAS_SSH_PORT="${NAS_SSH_PORT:-22}"
NAS_PHOTO_DIR="${NAS_PHOTO_DIR:-/volume1/photo}"
LOCAL_PHOTO_DIR="${PHOTO_LIB_DIR:-./photos}"

# IPv6 地址在 ssh/rsync 里用 [addr] 包裹
if [[ "$NAS_SSH_HOST" == *":"* ]]; then
  SSH_HOST="[${NAS_SSH_HOST}]"
else
  SSH_HOST="${NAS_SSH_HOST}"
fi

log() { echo "[sync_nas] $*"; }

[ -d "$LOCAL_PHOTO_DIR" ] || mkdir -p "$LOCAL_PHOTO_DIR"

ARGS=(-avz --partial --timeout=60 --exclude='@eaDir' --exclude='#recycle' \
      --exclude='.DS_Store' --exclude='Thumbs.db')
[ "${1:-}" = "--delete" ] && { ARGS+=(--delete); shift; }
[ "${1:-}" = "--dry-run" ] && ARGS+=(--dry-run)

log "同步 ${NAS_SSH_USER}@${SSH_HOST}:${NAS_PHOTO_DIR}/ -> ${LOCAL_PHOTO_DIR}/"
rsync "${ARGS[@]}" -e "ssh -p ${NAS_SSH_PORT} -o ConnectTimeout=15" \
      "${NAS_SSH_USER}@${SSH_HOST}:${NAS_PHOTO_DIR}/" "${LOCAL_PHOTO_DIR}/"

log "同步完成。接下来可分析：python tools/analyze_photos.py ${LOCAL_PHOTO_DIR} -j 4"
