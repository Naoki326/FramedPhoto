# 开发流程

## 一键准备环境

```bash
./scripts/setup.sh
```

## 固件开发循环（ESP32-S3 配套板）

```bash
cd firmware
source ~/esp/esp-idf/export.sh
idf.py set-target esp32s3        # 首次（目标芯片为配套板 ESP32-S3）
idf.py menuconfig                # WiFi SSID/密码、服务端 URL、EPD 引脚（默认已按配套板）
idf.py build
idf.py -p /dev/cu.usbserial-XXXX flash monitor
```

或直接用 `./scripts/flash.sh`。

## 服务端开发循环

```bash
cd services
source .venv/bin/activate
cp .env.example .env             # 按需修改
uvicorn app.main:app --reload
```

测试：`cd services && .venv/bin/python -m pytest tests/ -q`

## 图片转换 CLI

```bash
python tools/convert_image.py photo.jpg -o out.fps6 --preview out.png
```

## 里程碑状态

见 [docs/roadmap.md](docs/roadmap.md)。当前 **M1 已实现（驱动移植完成、编译/单测通过）**，
剩余工作为**真机验证**：烧录后屏幕应依次显示白屏 → 6 色横条 → 棋盘格循环。
