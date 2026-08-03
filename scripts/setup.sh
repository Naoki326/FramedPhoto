#!/usr/bin/env bash
# setup.sh — 一键准备开发环境
# 用法: ./scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> [1/3] 检查 ESP-IDF"
if [ -z "${IDF_PATH:-}" ]; then
  if [ -d "$HOME/esp/esp-idf" ]; then
    echo "    找到 ~/esp/esp-idf，后续需 source export.sh 后再用 idf.py"
  else
    echo "    未找到 ESP-IDF，请先安装：https://docs.espressif.com/projects/esp-idf"
    exit 1
  fi
fi

echo "==> [2/3] 准备服务端虚拟环境 (Python 3.10+)"
PY=""
for c in python3.13 python3.12 python3.11 python3.10; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -z "$PY" ] && PY="python3"
echo "    使用 $PY"
"$PY" -m venv services/.venv
services/.venv/bin/pip install -q --upgrade pip
services/.venv/bin/pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r services/requirements.txt pytest

echo "==> [3/3] 验证固件可编译"
if [ -d "$HOME/esp/esp-idf" ]; then
  (source "$HOME/esp/esp-idf/export.sh" >/dev/null 2>&1 && cd firmware && idf.py build >/dev/null) \
    && echo "    固件编译 OK" || echo "    固件编译失败（先执行 idf.py set-target esp32）"
fi

echo
echo "完成。下一步："
echo "  固件:   cd firmware && idf.py menuconfig (WiFi/服务端地址) && idf.py flash monitor"
echo "  服务:   cd services && source .venv/bin/activate && uvicorn app.main:app --reload"
