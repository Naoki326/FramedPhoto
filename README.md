# FramedPhoto

基于 **7.09 寸 E Ink Spectra 6 彩色电子纸模组（GDEB0709E01）** + **ESP32** 的数字相框。

一个把照片搬上彩色电子纸的端到端项目：ESP32 固件负责驱动 E Ink 屏幕和联网取图，Python 服务端负责图片转换、内容管理与 OTA 分发。

## 硬件

| 部件 | 型号 | 说明 |
| --- | --- | --- |
| 电子纸模组 | GDEB0709E01 | 7.09 寸，E Ink Spectra 6（6 色），1200x1600，SPI 接口 |
| 主控 | ESP32 开发板 | 配套开发板，ESP-IDF 开发 |
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
idf.py set-target esp32
idf.py menuconfig                 # 配置 WiFi SSID / 密码 / 服务端地址
idf.py build flash monitor
```

### 服务端（Python 3.9+）

```bash
cd services
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # 按需修改
uvicorn app.main:app --reload
```

## 当前状态

- [x] 仓库骨架与环境
- [ ] GDEB0709E01 驱动（SPI 初始化 / 刷新序列）—— 待官方资料包确认命令表
- [ ] 固件应用层（WiFi + 内容拉取 + 渲染）
- [ ] 服务端图片转换（Spectra 6 色板量化 + 抖动）
- [ ] 设备管理与 OTA

路线图见 [docs/roadmap.md](docs/roadmap.md)，架构设计见 [docs/architecture.md](docs/architecture.md)。

## License

MIT（详见 [LICENSE](LICENSE)）。固件中引用的第三方组件版权归各自作者所有。
