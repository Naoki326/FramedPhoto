# 路线图

## M0 — 环境与骨架（完成 ✅）
- [x] Git 仓库初始化、目录结构
- [x] ESP-IDF v6.0.x 环境确认，固件骨架可编译
- [x] 服务端 FastAPI 骨架，epd_image 转换模块
- [x] 官方资料下载并归档 → `docs/vendor/gdeb0709e01/`

## M1 — 点亮屏幕（驱动完成，待真机验证）
- [x] 官方 ESP32 示例源码解析：双 IC 结构、4bit/像素、init/刷新序列
- [x] 驱动移植进 `firmware/components/gdey_epd/`（双 CS、手动 GPIO、8bit 命令相位）
- [x] 目标芯片确认 **ESP32-S3 + 16MB flash**，分区表含双 OTA + storage
- [x] 固件演示模式：纯色 / 6 色横条 / 棋盘格（流式生成，600B RAM）
- [x] 服务端 FPS6 格式对齐（4bit 双 IC 布局），协议文档 `docs/protocol/fps6-format.md`
- [x] 固件编译通过（esp32s3）；服务端 11 个单测通过
- [ ] **真机验证**：烧录后屏幕依次显示 白屏 → 6 色横条 → 棋盘格 循环

## M2 — 联网取图（代码完成，待真机联调）
- [x] WiFi（NVS 存凭据 + menuconfig 默认值）
- [x] content_client：HTTP 拉取内容清单（cJSON）+ FPS6 下载到 storage 分区（SPIFFS）
- [x] 流式渲染：从文件两阶段读取（先 IC0 全行再 IC1 全行），600B 行缓冲
- [x] 设备模拟器 tools/simulate_device.py + 集成测试：固件读取逻辑与服务端格式一致
- [x] 固件编译通过（958KB，OTA 分区余 70%）；服务端 13 单测通过
- [ ] **真机联调**：WiFi 配置 → 服务端上传 → 设备显示

## M3 — 服务端完善（完成 ✅）
- [x] SQLite 持久化（devices / images / firmware 表）
- [x] 设备注册 / 心跳 / 状态查询 API（在线状态、固件版本、电量）
- [x] 内容管理 API：列表 / 删除 / 预览；content 接口支持设备附带上报
- [x] Web 管理台 `GET /`：上传、内容库（预览/删除）、设备、OTA 发布
- [x] OTA：固件上传（0xE9 魔数校验 + sha256）+ manifest 轮询 + 下载
- [x] API 文档 `docs/api.md`；24 个单测全绿
- [ ] 设备端 OTA 客户端（esp_ota 分区切换）—— 真机阶段实现并验证

## M4 — 打磨
- [ ] 低功耗（deep sleep 定时唤醒刷新）
- [ ] 断网重连 / 异常恢复
- [ ] 时间显示（RTC / NTP）
- [ ] 部署：docker-compose 一键起服务

## 资料清单（docs/vendor/gdeb0709e01/）

| 文件 | 说明 |
| --- | --- |
| GDEB0709E01_规格书.pdf | 官方数据手册（23 页，含引脚/电路/时序/光学特性） |
| E6设计须知_CN.pdf | Spectra 6 面板设计注意事项 |
| 固件烧录方式.pdf | 配套板固件烧录方法（WiFi/SD 模式） |
| esp32_example/ | 官方 ESP-IDF 示例源码（main/：GDEP133C02 驱动 + comm + usbcdc） |

> 预编译固件（WiFi/SD 卡模式 bin）、NeoFrame 安卓应用、Spectra6 USB 工具为配套板
> 成品方案附件，体积大且非本仓库开发所需，未入库；需要时可从产品页重新下载：
> <https://www.good-display.cn/product/729.html> → 相关资料下载。
