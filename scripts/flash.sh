#!/usr/bin/env bash
# flash.sh — 一键烧录并监视固件
# 用法: ./scripts/flash.sh [串口设备，默认自动检测]
set -euo pipefail
cd "$(dirname "$0")/../firmware"

if [ -z "${IDF_PATH:-}" ] && [ -d "$HOME/esp/esp-idf" ]; then
  source "$HOME/esp/esp-idf/export.sh" >/dev/null
fi

PORT="${1:-}"
if [ -z "$PORT" ]; then
  PORT=$(idf.py -p /dev/cu.usbserial* 2>/dev/null || true)
fi

idf.py "${PORT:+-p $PORT}" flash monitor
