#!/usr/bin/env bash
# mount_nas.sh — 挂载/卸载群晖 NAS 共享（SMB），作为照片数据源
#
# 用法：
#   ./scripts/mount_nas.sh mount    挂载（macOS 用 mount_smbfs；Linux 用 mount.cifs）
#   ./scripts/mount_nas.sh umount   卸载
#   ./scripts/mount_nas.sh status   查看挂载状态
#
# 配置：优先读环境变量，或 services/.env：
#   NAS_HOST=192.168.1.5          NAS 地址
#   NAS_SHARE=photo               共享名（群晖控制面板-文件服务-SMB 中设置）
#   NAS_USER=username             SMB 用户名
#   NAS_PASSWORD=                 SMB 密码（留空则交互输入；勿提交到 git）
#   NAS_MOUNT_POINT=/Volumes/framedphoto-nas   挂载点（必须固定！）
set -euo pipefail
cd "$(dirname "$0")/.."

# 从 services/.env 读取 NAS_*（不覆盖已导出的环境变量）
ENV_FILE="services/.env"
if [ -f "$ENV_FILE" ]; then
  set -a; source "$ENV_FILE"; set +a
fi

: "${NAS_HOST:?需要 NAS_HOST（如 192.168.1.5）}"
: "${NAS_SHARE:?需要 NAS_SHARE（群晖共享名）}"
: "${NAS_USER:?需要 NAS_USER}"
NAS_MOUNT_POINT="${NAS_MOUNT_POINT:-/Volumes/framedphoto-nas}"

log() { echo "[mount_nas] $*"; }

mount_it() {
  if mountpoint -q "$NAS_MOUNT_POINT" 2>/dev/null; then
    log "已在挂载状态: $NAS_MOUNT_POINT"; return 0
  fi

  sudo_mkdir() {
    mkdir -p "$NAS_MOUNT_POINT" 2>/dev/null || {
      echo "需要创建挂载点 $NAS_MOUNT_POINT（请输入管理员密码）"
      sudo mkdir -p "$NAS_MOUNT_POINT"
    }
  }

  if [ "$(uname)" = "Darwin" ]; then
    # macOS
    sudo_mkdir
    if [ -n "${NAS_PASSWORD:-}" ]; then
      mount_smbfs "//${NAS_USER}:${NAS_PASSWORD}@${NAS_HOST}/${NAS_SHARE}" "$NAS_MOUNT_POINT"
    else
      echo "请输入 SMB 密码（用户 ${NAS_USER}@${NAS_HOST}）:"
      read -r -s NAS_PASSWORD
      mount_smbfs "//${NAS_USER}:${NAS_PASSWORD}@${NAS_HOST}/${NAS_SHARE}" "$NAS_MOUNT_POINT"
    fi
  else
    # Linux（需要 cifs-utils 与 sudo）
    command -v mount.cifs >/dev/null || { echo "缺少 cifs-utils，请先: sudo apt install cifs-utils"; exit 1; }
    sudo_mkdir
    local opts="username=${NAS_USER},uid=$(id -u),gid=$(id -g),iocharset=utf8"
    if [ -n "${NAS_PASSWORD:-}" ]; then
      sudo mount -t cifs "//${NAS_HOST}/${NAS_SHARE}" "$NAS_MOUNT_POINT" -o "$opts,password=${NAS_PASSWORD}"
    else
      sudo mount -t cifs "//${NAS_HOST}/${NAS_SHARE}" "$NAS_MOUNT_POINT" -o "$opts"
    fi
  fi
  log "已挂载 ${NAS_HOST}/${NAS_SHARE} -> ${NAS_MOUNT_POINT}"
}

umount_it() {
  if mountpoint -q "$NAS_MOUNT_POINT" 2>/dev/null; then
    if [ "$(uname)" = "Darwin" ]; then
      diskutil unmount "$NAS_MOUNT_POINT"
    else
      sudo umount "$NAS_MOUNT_POINT"
    fi
    log "已卸载 $NAS_MOUNT_POINT"
  else
    log "未挂载（或挂载点不存在）"
  fi
}

status() {
  if mountpoint -q "$NAS_MOUNT_POINT" 2>/dev/null; then
    log "已挂载: $NAS_MOUNT_POINT（目标 ${NAS_HOST}/${NAS_SHARE}）"
    ls "$NAS_MOUNT_POINT" | head -5 | sed 's/^/    /'
  else
    log "未挂载: $NAS_MOUNT_POINT（执行 ./scripts/mount_nas.sh mount）"
  fi
}

case "${1:-status}" in
  mount)   mount_it ;;
  umount|unmount) umount_it ;;
  status)  status ;;
  *) echo "用法: $0 {mount|umount|status}"; exit 1 ;;
esac
