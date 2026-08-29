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
             "transferred_files", "total_files", "started_at", "last_sync",
             "avg_speed_kb"):
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

# ---------- 互斥锁（手动按钮与 launchd 不能并发 rsync） ----------
LOCK_DIR="${TMPDIR:-/tmp}/framedphoto-sync-nas.lock"
acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    printf '%s\n' "$$" > "$LOCK_DIR/pid"
    trap 'rm -rf "$LOCK_DIR"' EXIT
    return 0
  fi
  local pid=""
  [ -f "$LOCK_DIR/pid" ] && pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    log "已有同步任务运行中（pid $pid），本次跳过"
    exit 75
  fi
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR"
  printf '%s\n' "$$" > "$LOCK_DIR/pid"
  trap 'rm -rf "$LOCK_DIR"' EXIT
}

acquire_lock

# 本次同步开始时刻：relocate-post 的孤儿判定 cutoff
# （晚于此落盘的本地文件视为待推送 NAS 的上传，不入回收站）
SYNC_START_TS=$(date +%s)
# 远端清单临时文件（exit 时与锁一起清理）
MANIFEST="$(mktemp /tmp/framedphoto-manifest.XXXXXX)"
trap 'rm -rf "$LOCK_DIR"; rm -f "$MANIFEST"' EXIT

# ---------- 远端图片清单（排除视频）：进度分母 + 重组收敛依据 ----------
# SSH keepalive：长连接（限速下可达数小时）中途空闲易被 NAT/对端断开，
# 每 30s 发保活包，4 次无响应才判定死连接
SSH_KEEPALIVE=(-o ServerAliveInterval=30 -o ServerAliveCountMax=4)

fetch_remote_manifest() {
  ssh -p "$NAS_SSH_PORT" -o ConnectTimeout=15 "${SSH_KEEPALIVE[@]}" \
    "${NAS_SSH_USER}@${SSH_HOST}" \
    "cd '${NAS_PHOTO_DIR}' && find . -type f \
       ! -path '*/@eaDir/*' ! -path '*/#recycle/*' ! -path '*/N8BookData/*' \
       \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.gif' \
          -o -iname '*.heic' -o -iname '*.heif' -o -iname '*.webp' -o -iname '*.bmp' \
          -o -iname '*.tif' -o -iname '*.tiff' -o -iname '*.cr2' -o -iname '*.nef' \
          -o -iname '*.arw' -o -iname '*.dng' -o -iname '*.raf' -o -iname '*.orf' \
          -o -iname '*.rw2' -o -iname '*.pef' -o -iname '*.srw' -o -iname '*.x3f' \) \
       -exec stat -c '%s|%Y|%n' {} \; 2>/dev/null" \
    || true
}

# 白名单：只同步图片文件（相框只需照片），其余一律排除。
# 共享过滤规则会匹配大小写扩展名；非图片内容由最后的 --exclude='*' 排除。
# 不按业务目录名排除，避免「婚礼视频」等目录里的照片被一起漏掉。
source "$(dirname "$0")/sync_nas_filters.sh"

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

# ---------- 启动：拉清单、还原重组、状态初始化 ----------
log "同步 ${NAS_SSH_USER}@${SSH_HOST}:${NAS_PHOTO_DIR}/ -> ${LOCAL_PHOTO_DIR}/"
fetch_remote_manifest > "$MANIFEST"
TOTAL_FILES=$(wc -l < "$MANIFEST" | tr -d ' ')
TOTAL_BYTES=$(awk -F'|' '{s+=$1} END {print s+0}' "$MANIFEST")
log "远程图片: ${TOTAL_FILES:-0} 个文件 / $(( ${TOTAL_BYTES:-0} / 1024 / 1024 )) MB"

# 阶段一（rsync 前）：NAS 重组过目录时，把本地文件按 (basename,size,mtime)
# 挪到远端新路径，rsync 只传真正新增的照片（不再全量重传）。
# 清单为空 = SSH 失败或 NAS 库被清空：跳过收敛（保守，不误删不误挪）。
RELOCATE_PY="${RELOCATE_PY:-services/.venv/bin/python}"
if [ -s "$MANIFEST" ] && [ "$DRY_RUN" -eq 0 ]; then
  if OUT=$(PYTHONPATH=services "$RELOCATE_PY" -m app.nas_sync relocate-pre \
        --manifest "$MANIFEST" --photos "$LOCAL_PHOTO_DIR" 2>&1); then
    log "$OUT"
  else
    log "警告：relocate-pre 失败（继续 rsync，仅损失流量优化）：$OUT"
  fi
fi
if [ "$DRY_RUN" -eq 1 ]; then
  write_status \
    "status" "running" "percent" 0 \
    "total_files" "${TOTAL_FILES:-0}" "total_bytes" "${TOTAL_BYTES:-0}" \
    "transferred_files" 0 "transferred_bytes" 0 \
    "bwlimit_kb" "$NAS_RSYNC_BWLIMIT" \
    "message" "演练模式（不传输）" "started_at" "$(date +%s)"
else
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
# 平均速度滑动窗口：最近 10 次采样（每 5s 一次 ≈ 50s），下载量 ÷ 时间
SPEED_BYTES=()
SPEED_TS=()
while kill -0 "$RSYNC_PID" 2>/dev/null; do
  LAST=$(tr '\r' '\n' < "$RSYNC_LOG" | grep '%' | tail -1)
  PERCENT=$(echo "$LAST" | grep -oE '[0-9]+%' | tail -1 | tr -d '%')
  BYTES=$(echo "$LAST" | awk '{print $1}' | tr -d ',')
  XFR=$(echo "$LAST" | grep -oE 'xfr#[0-9]+' | grep -oE '[0-9]+')
  # 采样推进（无论是否有新日志行，保证窗口按 5s 推进）
  SPEED_BYTES+=("${BYTES:-0}")
  SPEED_TS+=("$(date +%s)")
  if [ "${#SPEED_BYTES[@]}" -gt 10 ]; then
    SPEED_BYTES=("${SPEED_BYTES[@]:1}")
    SPEED_TS=("${SPEED_TS[@]:1}")
  fi
  AVG_SPEED_KB=""
  if [ "${#SPEED_BYTES[@]}" -ge 2 ]; then
    IDX=$(( ${#SPEED_BYTES[@]} - 1 ))
    D_B=$(( SPEED_BYTES[$IDX] - SPEED_BYTES[0] ))
    D_T=$(( SPEED_TS[$IDX] - SPEED_TS[0] ))
    if [ "$D_T" -gt 0 ] && [ "$D_B" -ge 0 ]; then
      AVG_SPEED_KB=$(awk "BEGIN{printf \"%.1f\", $D_B/1024/$D_T}")
    fi
  fi
  if [ -n "$PERCENT" ]; then
    write_status \
      "percent" "${PERCENT}" \
      "transferred_bytes" "${BYTES:-0}" \
      "transferred_files" "${XFR:-0}" \
      "avg_speed_kb" "${AVG_SPEED_KB}" \
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
                 "avg_speed_kb" "${AVG_SPEED_KB}" \
                 "last_sync" "$(date +%s)"
    log "同步完成。接下来可分析：python tools/analyze_photos.py ${LOCAL_PHOTO_DIR} -j 4"
    # 阶段二（rsync 后）：镜像收敛（ADR-0007）——NAS 已删的本地孤儿入回收站
    # （services/.sync_trash，保留 7 天），photo_scores 失效路径按内容哈希
    # 迁移、无主记录删除（否则管理台照片库裂图）。
    if [ -s "$MANIFEST" ]; then
      if OUT=$(PYTHONPATH=services "$RELOCATE_PY" -m app.nas_sync relocate-post \
            --manifest "$MANIFEST" --photos "$LOCAL_PHOTO_DIR" \
            --db services/framedphoto.db --trash services/.sync_trash \
            --cutoff "$SYNC_START_TS" 2>&1); then
        log "$OUT"
      else
        log "警告：relocate-post 失败（孤儿/评分未收敛，可重跑同步）：$OUT"
      fi
    fi
    # 自动分析（默认开，NAS_AUTO_ANALYZE=0 关闭）：同步后自动跑启发式评分，
    # 让新同步的照片（如各用户 Frame_* 文件夹）直接进照片库参与分类与选片。
    # 启发式本地计算不花钱（VLM 分析仍手动），断点续跑只分析新增未分析照片。
    if [ "${NAS_AUTO_ANALYZE:-1}" = "1" ]; then
      log "自动启发式分析 ${LOCAL_PHOTO_DIR} …"
      # analyze_photos.py 内部 os.chdir 到 services/，须传绝对路径（相对路径会失效）
      ABS_LIB="$(cd "${LOCAL_PHOTO_DIR}" && pwd)"
      if services/.venv/bin/python "$(pwd)/tools/analyze_photos.py" "${ABS_LIB}" --no-vlm -j "${NAS_ANALYZE_JOBS:-4}"; then
        log "自动分析完成，新照片已进照片库"
      else
        log "自动分析失败（照片仍在照片库，可手动重跑）"
      fi
    fi
  fi
else
  write_status "status" "error" "message" "同步失败（rsync 退出码 ${RC}，详见 ${RSYNC_LOG}）"
  log "同步失败: rsync 退出码 $RC，日志: $RSYNC_LOG"
  exit "$RC"
fi
