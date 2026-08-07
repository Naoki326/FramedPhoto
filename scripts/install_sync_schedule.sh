#!/usr/bin/env bash
# install_sync_schedule.sh — 用 macOS launchd 定时运行 NAS 增量同步。
# 默认：登录/加载时运行一次，之后每 3600 秒运行一次。
set -euo pipefail

LABEL="com.framedphoto.nas-sync"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_DIR/scripts/sync_nas.sh"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG="${REPO_DIR}/services/sync_nas_launchd.log"
INTERVAL="${SYNC_INTERVAL:-3600}"

case "$INTERVAL" in
  ''|*[!0-9]*) echo "SYNC_INTERVAL 必须是秒数" >&2; exit 2 ;;
  0) echo "SYNC_INTERVAL 必须大于 0" >&2; exit 2 ;;
esac

write_plist() {
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${SCRIPT}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${REPO_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>StartInterval</key>
    <integer>${INTERVAL}</integer>
    <key>ThrottleInterval</key>
    <integer>60</integer>
    <key>ProcessType</key>
    <string>Background</string>
    <key>LowPriorityIO</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG}</string>
    <key>StandardErrorPath</key>
    <string>${LOG}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
EOF
  echo "[install] 已生成 ${PLIST}（每 ${INTERVAL} 秒）"
}

cmd_install() {
  [ -x "$SCRIPT" ] || { echo "同步脚本不可执行: $SCRIPT" >&2; exit 1; }
  write_plist
  launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || {
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
  }
  echo "[install] 已加载并启动 ${LABEL}"
  cmd_status
}

cmd_start() { launchctl kickstart -k "gui/$(id -u)/${LABEL}"; }
cmd_stop() { launchctl kill SIGTERM "gui/$(id -u)/${LABEL}" 2>/dev/null || true; }
cmd_status() { launchctl print "gui/$(id -u)/${LABEL}" 2>/dev/null | head -18 || echo "[warn] 定时任务未加载"; }
cmd_uninstall() {
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  rm -f "$PLIST"
  echo "[uninstall] 已卸载 ${LABEL}"
}

case "${1:-install}" in
  install)   cmd_install ;;
  start)     cmd_start ;;
  stop)      cmd_stop ;;
  status)    cmd_status ;;
  uninstall) cmd_uninstall ;;
  *) echo "用法: $0 {install|start|stop|status|uninstall}"; exit 1 ;;
esac
