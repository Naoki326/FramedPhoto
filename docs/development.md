# 开发流程

## 一键准备环境

```bash
./scripts/setup.sh
```

## 固件开发循环

```bash
cd firmware
source ~/esp/esp-idf/export.sh
idf.py set-target esp32          # 首次
idf.py menuconfig                # WiFi SSID/密码、服务端 URL、EPD 引脚
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

见 [docs/roadmap.md](docs/roadmap.md)。当前处于 **M0/M1 交界**：
- M1 需要官方资料包（数据手册）才能填充 `gdeb0709e01.c` 的 init/刷新序列。
- 拿到资料后第一步：把命令序列填入驱动组件，烧录跑「点亮屏幕」测试。
