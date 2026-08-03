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

## launchd 部署（macOS 开机自启 + 内网访问）

```bash
# 部署并启动（绑定 0.0.0.0，内网设备可访问，崩溃自动重启）
./scripts/install_service.sh install

# 常用命令
./scripts/install_service.sh status     # 查看状态
./scripts/install_service.sh restart    # 重启
./scripts/install_service.sh uninstall  # 卸载

# 日志：services/framedphoto.log
# 访问：http://<本机内网IP>:8010（管理台）/ /health（健康检查）
```

> ⚠️ 重要：**项目不要放在 macOS 隐私保护目录**（`~/Documents`、`~/Desktop`、
> `~/Downloads`）下，launchd 代理进程访问这些目录会被 TCC 拦截导致服务起不来。
> 推荐放在 `~/FramedPhoto` 这类普通目录。

## 图片转换 CLI

```bash
python tools/convert_image.py photo.jpg -o out.fps6 --preview out.png
```

## 里程碑状态

见 [docs/roadmap.md](docs/roadmap.md)。当前 **M1~M4 代码完成**：

- M1 驱动移植（双 IC 命令序列、编译通过）
- M2 联网取图（内容清单 + FPS6 下载 + 流式渲染）
- M3 服务端（SQLite / 设备管理 / Web 管理台 / OTA）
- M4 AI 回忆相框（照片分析评分 + 历史上的今天每日精选 + 文案渲染 + 深度休眠）
- 服务端 37 个单测全绿，固件编译通过

**真机验证**（硬件到手后）：
1. `idf.py menuconfig` 配置 WiFi SSID/密码和服务端 URL
2. 烧录 `./scripts/flash.sh`
3. 无网显示棋盘格；联网后拉取服务端内容（手动上传或每日精选）
4. 显示后按 `FRAMEDPHOTO_DEEP_SLEEP_HOURS` 深度休眠，RTC 定时唤醒
