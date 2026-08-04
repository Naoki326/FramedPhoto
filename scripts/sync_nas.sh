#!/usr/bin/env bash
# sync_nas.sh — 从远程群晖 NAS 增量同步照片到本机（rsync over SSH，IPv6 友好）
#
# 适用场景：NAS 在远程（IPv6 公网 / 公网 IP），相比 SMB 直连：
#   - SSH 加密，不暴露 SMB 445 端口
#   - 增量同步：只传新增/变化的照片，日常流量极小
#   - 断线自动重试，适合公网链路
#
# 进度上报：运行期间写入 services/sync_status.json（Web 管理台显示）
#
# 用法：
#   ./scripts/sync_nas.sh            同步（增量）
#   ./scripts/sync_nas.sh --dry-run  演练（不实际传输）
#   ./scripts/sync_nas.sh --delete   同步并删除本机已不在 NAS 的照片
#
# 配置（services/.env）：
#   NAS_SSH_HOST                     NAS 的 SSH config 别名 / IPv6 / 域名
#   NAS_SSH_USER                      SSH 用户（群晖管理员）
#   NAS_SSH_PORT=22
#   NAS_PHOTO_DIR=/volume1/photo/相片 远程照片目录
#   NAS_RSYNC_PATH=/usr/local/bin/rsync  群晖需指向 synocli-net 的真实 rsync
#   NAS_RSYNC_BWLIMIT=100            限速 KB/s（0 = 不限）
#   PHOTO_LIB_DIR=./photos           本机同步目录（默认 services/photos）
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
NAS_RSYNC_BWLIMIT="${NAS_RSYNC_BWLIMIT:-0}"   # KB/s，0 = 不限速
NAS_RSYNC_PATH="${NAS_RSYNC_PATH:-}"        # 群晖需指向真实 rsync（如 /usr/local/bin/rsync）
LOCAL_PHOTO_DIR="${PHOTO_LIB_DIR:-services/photos}"
# 相对路径按 services/ 解析（服务端 cwd 为 services）
case "$LOCAL_PHOTO_DIR" in
  /*) ;;
  *) LOCAL_PHOTO_DIR="services/$LOCAL_PHOTO_DIR" ;;
esac

STATUS_FILE="services/sync_status.json"
RSYNC_LOG="services/sync_nas.log"

log() { echo "[sync_nas] $*"; }

# ---------- 状态上报（Web 管理台读取） ----------
# 用法：write_status "percent" 42 "message" "同步中"
write_status() {
  /usr/bin/python3 - "$STATUS_FILE" "$@" <<'PY'
import json, os, sys, time
path, pairs = sys.argv[1], sys.argv[2:]
data = {}
if os.path.exists(path):
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception:
        pass
for i in range(0, len(pairs), 2):
    k, v = pairs[i], pairs[i + 1]
    if k in ("percent", "transferred_bytes", "total_bytes",
             "transferred_files", "total_files", "started_at", "last_sync"):
        try:
            v = float(v) if "." in str(v) else int(v)
        except ValueError:
            pass
    data[k] = v
data["updated_at"] = time.time()
tmp = path + ".tmp"
json.dump(data, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
os.replace(tmp, path)
PY
}

# ---------- 远程照片统计（排除视频，进度分母） ----------
# SSH keepalive：长连接（限速下可达数小时）中途空闲易被 NAT/对端断开，
# 每 30s 发保活包，4 次无响应才判定死连接
SSH_KEEPALIVE=(-o ServerAliveInterval=30 -o ServerAliveCountMax=4)

fetch_remote_stats() {
  ssh -p "$NAS_SSH_PORT" -o ConnectTimeout=15 "${SSH_KEEPALIVE[@]}" \
    "${NAS_SSH_USER}@${SSH_HOST}" \
    "cd '${NAS_PHOTO_DIR}' && find . -type f \
       ! -name '*.mp4' ! -name '*.mov' ! -name '*.avi' ! -name '*.mkv' \
       ! -name '*.zip' ! -name '*.rar' ! -name '*.7z' ! -name '*.tar' \
       -exec stat -c '%s' {} \; 2>/dev/null | awk '{s+=\$1; n++} END {print n, s}'" \
    || echo "0 0"
}

# 默认排除：视频/压缩包/系统目录（相框只需照片，省流量）
EXCLUDES=(
  --exclude='@eaDir' --exclude='#recycle' --exclude='.DS_Store' --exclude='Thumbs.db'
  --exclude='*.mp4' --exclude='*.mov' --exclude='*.avi' --exclude='*.mkv'
  --exclude='*.zip' --exclude='*.rar' --exclude='*.7z' --exclude='*.tar'
)

# IPv6 地址在 ssh/rsync 里用 [addr] 包裹
if [[ "$NAS_SSH_HOST" == *":"* ]]; then
  SSH_HOST="[${NAS_SSH_HOST}]"
else
  SSH_HOST="${NAS_SSH_HOST}"
fi

# macOS 自带 openrsync 与群晖 rsync 协议不兼容，优先用 brew 官方 rsync
RSYNC_BIN="${RSYNC_BIN:-}"
if [ -z "$RSYNC_BIN" ]; then
  for cand in /opt/homebrew/bin/rsync /usr/local/bin/rsync; do
    if [ -x "$cand" ]; then RSYNC_BIN="$cand"; break; fi
  done
fi
RSYNC_BIN="${RSYNC_BIN:-rsync}"
log "使用 rsync: $RSYNC_BIN ($($RSYNC_BIN --version | head -1))"

[ -d "$LOCAL_PHOTO_DIR" ] || mkdir -p "$LOCAL_PHOTO_DIR"

ARGS=(-rltz --partial --timeout=60 --chmod=Du+rwx,Dg+rx,Fu+rw,Fgo+r "${EXCLUDES[@]}")
if [ "$NAS_RSYNC_BWLIMIT" -gt 0 ] 2>/dev/null; then
  ARGS+=(--bwlimit="$NAS_RSYNC_BWLIMIT")
fi
[ "${1:-}" = "--delete" ] && { ARGS+=(--delete); shift; }
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && { ARGS+=(--dry-run); DRY_RUN=1; shift; }

# ---------- 启动：状态初始化 ----------
log "同步 ${NAS_SSH_USER}@${SSH_HOST}:${NAS_PHOTO_DIR}/ -> ${LOCAL_PHOTO_DIR}/"
if [ "$DRY_RUN" -eq 1 ]; then
  write_status "status" "running" "message" "演练模式（不传输）" "started_at" "$(date +%s)"
else
  STATS=$(fetch_remote_stats)
  TOTAL_FILES=$(echo "$STATS" | awk '{print $1}')
  TOTAL_BYTES=$(echo "$STATS" | awk '{print $2}')
  log "远程照片: ${TOTAL_FILES} 个文件 / $((TOTAL_BYTES / 1024 / 1024)) MB"
  write_status \
    "status" "running" "percent" 0 \
    "total_files" "${TOTAL_FILES:-0}" "total_bytes" "${TOTAL_BYTES:-0}" \
    "transferred_files" 0 "transferred_bytes" 0 \
    "bwlimit_kb" "$NAS_RSYNC_BWLIMIT" \
    "message" "开始同步" "started_at" "$(date +%s)"
fi

# ---------- rsync（后台）+ 进度追踪 ----------
: > "$RSYNC_LOG"
RSYNC_OPTS=(-e "ssh -p ${NAS_SSH_PORT} -o ConnectTimeout=15 ${SSH_KEEPALIVE[*]}")
[ -n "$NAS_RSYNC_PATH" ] && RSYNC_OPTS+=(--rsync-path="$NAS_RSYNC_PATH")

"$RSYNC_BIN" "${ARGS[@]}" --info=progress2 "${RSYNC_OPTS[@]}" \
      "${NAS_SSH_USER}@${SSH_HOST}:${NAS_PHOTO_DIR}/" "${LOCAL_PHOTO_DIR}/" \
      > "$RSYNC_LOG" 2>&1 &
RSYNC_PID=$!

# 进度循环：解析 rsync progress2 输出，持续更新状态
# 注意：列文件清单阶段日志无 '%'，grep 返回非零；set -e 下会误杀脚本，
# 因此循环与 wait 全程关闭 errexit（rsync 退出码由 RC 显式捕获）
set +e
while kill -0 "$RSYNC_PID" 2>/dev/null; do
  LAST=$(tr '\r' '\n' < "$RSYNC_LOG" | grep '%' | tail -1)
  PERCENT=$(echo "$LAST" | grep -oE '[0-9]+%' | tail -1 | tr -d '%')
  BYTES=$(echo "$LAST" | awk '{print $1}' | tr -d ',')
  XFR=$(echo "$LAST" | grep -oE 'xfr#[0-9]+' | grep -oE '[0-9]+')
  if [ -n "$PERCENT" ]; then
    write_status \
      "percent" "${PERCENT}" \
      "transferred_bytes" "${BYTES:-0}" \
      "transferred_files" "${XFR:-0}" \
      "message" "同步中（限速 ${NAS_RSYNC_BWLIMIT} KB/s）"
  fi
  sleep 5
done
wait "$RSYNC_PID"; RC=$?
set -e

# ---------- 结束：写入最终状态 ----------
if [ "$RC" -eq 0 ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    write_status "status" "done" "message" "演练完成（未传输）"
  else
    write_status "status" "done" "percent" 100 "message" "同步完成" \
                 "last_sync" "$(date +%s)"
    log "同步完成。接下来可分析：python tools/analyze_photos.py ${LOCAL_PHOTO_DIR} -j 4"
  fi
else
  write_status "status" "error" "message" "同步失败（rsync 退出码 ${RC}，详见 ${RSYNC_LOG}）"
  log "同步失败: rsync 退出码 $RC，日志: $RSYNC_LOG"
  exit "$RC"
fi
