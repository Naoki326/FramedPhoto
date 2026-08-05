# FramedPhoto

基于 **7.09 寸 E Ink Spectra 6 彩色电子纸模组（GDEB0709E01）** + **ESP32** 的数字相框。

一个把照片搬上彩色电子纸的端到端项目：ESP32 固件负责驱动 E Ink 屏幕和联网取图，Python 服务端负责图片转换、内容管理与 OTA 分发。

## 硬件

| 部件 | 型号 | 说明 |
| --- | --- | --- |
| 电子纸模组 | GDEB0709E01 | 7.09 寸，E Ink Spectra 6（6 色），1200x1600，SPI 接口 |
| 主控 | ESP32-S3 开发板 | 配套开发板，16MB flash（双 OTA 分区），ESP-IDF v6.0.2 开发 |
| 电源 | 3.3V | 电子纸刷新瞬态电流较大，供电需留余量 |

详细规格与引脚映射见 [docs/hardware/gdeb0709e01.md](docs/hardware/gdeb0709e01.md)。

## 仓库结构

```
FramedPhoto/
├── firmware/          # ESP-IDF 固件（ESP32）
│   ├── main/          #   应用层：WiFi / 内容拉取 / 显示任务
│   └── components/    #   组件：gdey_epd 电子纸驱动
├── services/          # FastAPI 服务端
│   └── app/           #   图片转换 / 设备管理 / 内容推送 / OTA
├── docs/              # 硬件资料、架构、路线图
├── tools/             # 辅助脚本（图片转换 CLI、烧录脚本等）
└── scripts/           # 开发便利脚本
```

## 快速开始

### 固件（ESP-IDF v6.0.x）

```bash
cd firmware
source $IDF_PATH/export.sh        # 或 ~/esp/esp-idf/export.sh
idf.py set-target esp32s3         # 配套板目标芯片为 ESP32-S3
idf.py menuconfig                 # 配置 WiFi SSID / 密码 / 服务端地址 / 休眠周期
idf.py build flash monitor
```

### 服务端（Python 3.12）

```bash
cd services
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # 按需修改（VLM / NAS / 端口）
uvicorn app.main:app --reload
```

生产部署（macOS launchd 开机自启 + 内网访问）：`./scripts/install_service.sh install`，详见 [docs/development.md](docs/development.md)。

## 当前状态

- [x] 仓库骨架与环境
- [x] 官方资料下载归档（规格书 / 设计须知 / ESP32 示例源码，见 `docs/vendor/`）
- [x] **M1 驱动移植**（双 IC 命令序列 → `gdey_epd`，ESP32-S3，编译通过）
- [x] **M2 联网取图**（内容清单 + FPS6 下载 + 流式渲染，模拟器验证）
- [x] **M3 服务端**（SQLite、设备管理、Web 管理台、OTA，45 单测全绿）
- [x] **M4 AI 回忆相框**：AI 照片评分（gpt-5.6-luna via Responses API）、历史上的今天每日精选、文案渲染、设备深度休眠
- [x] **自由模块**：新闻版块升级为可配置的图文模块——一段 prompt 每天生成不同内容（宝宝背单词 / 读诗 / 历史故事 / 李白的诗和酒 / 童话故事…），LLM 定当日内容 + 即梦配插画 + 屏幕叠加文字，可多模块每日轮换
- [x] **群晖 NAS 数据源**：rsync over SSH 增量同步 + 掉线容错（见 `docs/nas.md`）
- [ ] **NAS 首轮同步 + 全量 AI 分析**（1200+ 张，`tools/analyze_photos.py`）
- [ ] **真机验证**（烧录：棋盘格 → WiFi → 服务端图片 → 每日精选 → 深度休眠；硬件到手后）
- [x] **设备端 OTA 客户端**（esp_ota 分区切换 + sha256 校验 + 失败回滚）

AI 能力见 [docs/ai-features.md](docs/ai-features.md)，NAS 接入见 [docs/nas.md](docs/nas.md)，API 见 [docs/api.md](docs/api.md)。

> 服务端当前以 launchd 服务运行在 `http://<内网IP>:8010`（端口 8000 被其他服务占用）。

路线图见 [docs/roadmap.md](docs/roadmap.md)，架构设计见 [docs/architecture.md](docs/architecture.md)。

## License

MIT（详见 [LICENSE](LICENSE)）。固件中引用的第三方组件版权归各自作者所有。
