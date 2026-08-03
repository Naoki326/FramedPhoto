#!/usr/bin/env bash
# install_service.sh — 用 launchd 部署 FramedPhoto 服务（macOS）
#
# 功能：
#   - 生成并安装 LaunchAgent plist（用户级，开机自启 + 崩溃自动重启）
#   - uvicorn 绑定 0.0.0.0，内网设备（相框/手机/电脑）可访问
#   - 日志写入 services/framedphoto.log
#
# 用法：
#   ./scripts/install_service.sh install   部署并启动（默认）
#   ./scripts/install_service.sh start     启动
#   ./scripts/install_service.sh stop      停止
#   ./scripts/install_service.sh restart   重启
#   ./scripts/install_service.sh status    状态
#   ./scripts/install_service.sh uninstall 卸载（删除 plist）
set -euo pipefail

LABEL="com.framedphoto.service"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICES_DIR="$REPO_DIR/services"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG="$SERVICES_DIR/framedphoto.log"
PORT="${APP_PORT:-8010}"

uvicorn_bin() {
  [ -x "$SERVICES_DIR/.venv/bin/uvicorn" ] && echo "$SERVICES_DIR/.venv/bin/uvicorn" || command -v uvicorn
}

write_plist() {
  local UVICORN BIN_PATH
  UVICORN="$(uvicorn_bin)"
  BIN_PATH="$(dirname "$UVICORN")"
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
        <string>${UVICORN}</string>
        <string>app.main:app</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>${PORT}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${SERVICES_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG}</string>
    <key>StandardErrorPath</key>
    <string>${LOG}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${BIN_PATH}:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
EOF
  echo "[install] 已生成 $PLIST"
}

cmd_install() {
  write_plist
  launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || {
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
  }
  echo "[install] 已加载并启动 ${LABEL}"
  cmd_status
}

cmd_start()   { launchctl kickstart -k "gui/$(id -u)/${LABEL}"; echo "[ok] 已启动"; }
cmd_stop()    { launchctl kill SIGTERM "gui/$(id -u)/${LABEL}" 2>/dev/null || echo "[warn] 未在运行"; }
cmd_restart() { cmd_stop; sleep 1; cmd_start; }
cmd_status()  { launchctl print "gui/$(id -u)/${LABEL}" 2>/dev/null | head -12 || echo "[warn] 服务未加载"; }
cmd_uninstall() {
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  rm -f "$PLIST"
  echo "[uninstall] 已卸载（plist 已删除）"
}

case "${1:-install}" in
  install)   cmd_install ;;
  start)     cmd_start ;;
  stop)      cmd_stop ;;
  restart)   cmd_restart ;;
  status)    cmd_status ;;
  uninstall) cmd_uninstall ;;
  *) echo "用法: $0 {install|start|stop|restart|status|uninstall}"; exit 1 ;;
esac
