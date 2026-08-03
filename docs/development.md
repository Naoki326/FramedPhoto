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

见 [docs/roadmap.md](docs/roadmap.md)。当前 **M1+M2 代码完成**：
- M1 驱动移植完成（双 IC 命令序列、编译通过）
- M2 联网取图完成（内容清单拉取 + FPS6 下载 + 流式渲染，编译通过）
- 无真机验证通过：服务端 13 单测 + 设备模拟器对照（固件读取逻辑与服务端格式一致）

**真机验证**（硬件到手后）：
1. `idf.py menuconfig` 配置 WiFi SSID/密码和服务端 URL
2. 烧录 `./scripts/flash.sh`
3. 无网时屏显棋盘格；配好 WiFi 后从服务端拉图显示
