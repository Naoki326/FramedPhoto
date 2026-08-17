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

固件 `http_util` 传输层有宿主机 C 测试（无需 ESP-IDF 环境与真机，需 managed_components
已拉取）：`make -C firmware/host_tests`。

## 服务端开发循环

```bash
cd services
source .venv/bin/activate
cp .env.example .env             # 按需修改
uvicorn app.main:app --reload
```

测试：`cd services && .venv/bin/python -m pytest tests/ -q`

> ⚠️ 服务端 `Settings` 已设 `extra="ignore"`：`.env` 中脚本专用配置
> （`NAS_*` 等）不会导致加载失败，勿删此配置。

## launchd 部署（macOS 开机自启 + 内网访问）

```bash
# 部署并启动（绑定 0.0.0.0，内网设备可访问，崩溃自动重启）
./scripts/install_service.sh install

# 常用命令
./scripts/install_service.sh status     # 查看状态
./scripts/install_service.sh restart    # 重启
./scripts/install_service.sh uninstall  # 卸载

# 日志：services/framedphoto.log
# 访问：http://chenMac-mini.local:8010（管理台）/ /health（健康检查）
# 说明：.local 为 mDNS 主机名（scutil --get LocalHostName），电脑 IP 变化不影响访问
```

> ⚠️ 重要：**项目不要放在 macOS 隐私保护目录**（`~/Documents`、`~/Desktop`、
> `~/Downloads`）下，launchd 代理进程访问这些目录会被 TCC 拦截导致服务起不来。
> 推荐放在 `~/FramedPhoto` 这类普通目录。

## 运行中的服务

### launchd 服务（com.framedphoto.service）

- 开机自启 + KeepAlive（崩溃自动重启），绑定 0.0.0.0:8010
- 管理：`./scripts/install_service.sh {install|status|restart|stop|uninstall}`
- 访问：`http://chenMac-mini.local:8010`（管理台，mDNS 名，IP 变化不影响）；日志 `services/framedphoto.log`

> ⚠️ 排查时注意：项目目录移动过（如从 `~/Documents/Works/FramedPhoto` 迁到 `~/FramedPhoto`）
> 后，旧目录下可能残留手动启动的 uvicorn 进程（`lsof -i :端口` 可查），
> 其指向已不存在的代码路径（页面 500），记得 `kill` 掉避免占端口/混淆访问入口。

### 天气城市设置

管理台「天气卡片风格」下方城市输入框：输入中文城市名（如 `上海宝山`）保存时自动解析为和风城市 ID（`/api/weather/lookup`），也支持直接填城市 ID / 经纬度 / 拼音；留空回退 IP 自动定位。天气缓存按 location 指纹，改城市后立即生效。

### NAS 照片同步（sync_nas.sh）

- 增量同步在后台运行时，状态写入 `services/sync_status.json`（约每 5 秒更新一次），
  Web 管理台「NAS 照片同步」板块同步展示进度
- 日志：`/tmp/sync_nas.log`（脚本运行日志）与 `services/sync_nas.log`（rsync 明细）
- 完成后状态变为 `done`；同步细节与踩坑见 [docs/nas.md](docs/nas.md)

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
- 服务端 **105+ 个单测**（107 collected）全绿，固件编译通过（esp32s3，约 958KB）

**真机验证**（硬件到手后）：
1. `idf.py menuconfig` 配置 WiFi SSID/密码和服务端 URL
2. 烧录 `./scripts/flash.sh`
3. 无网显示棋盘格；联网后拉取服务端内容（手动上传或每日精选）
4. 显示后按 `FRAMEDPHOTO_DEEP_SLEEP_MIN` 深度休眠，RTC 定时唤醒
