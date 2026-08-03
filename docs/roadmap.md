# 路线图

## M0 — 环境与骨架（完成 ✅）
- [x] Git 仓库初始化、目录结构
- [x] ESP-IDF v6.0.x 环境确认，固件骨架可编译
- [x] 服务端 FastAPI 骨架，epd_image 转换模块（6 色量化 + 抖动）测试通过
- [x] 官方资料全部下载并归档（规格书 / 设计须知 / ESP32 程序示例源码 → `docs/vendor/gdeb0709e01/`）

## M1 — 点亮屏幕（进行中）
- [x] 官方 ESP32 示例源码解析完成：双 IC 结构、4bit/像素格式、init/刷新序列已整理进 `docs/hardware/gdeb0709e01.md`
- [ ] 将官方命令序列移植进 `firmware/components/gdey_epd/`（双 CS + 手动 GPIO + 8bit 命令相位 SPI）
- [ ] 设备端图像布局适配（每行 600B = IC0 左 300B + IC1 右 300B，行内右→左）
- [ ] 烧录验证：色块 / 测试图能正确点亮屏幕
- [ ] 服务端 FPS6 格式改为 4bit/像素（当前是 1 字节/像素，需要重写以匹配设备端）

## M2 — 联网取图
- [ ] WiFi（NVS 存凭据 + menuconfig 默认值）
- [ ] HTTP 拉取内容清单与图片（JSON + 二进制）
- [ ] 图片解码 → 帧缓冲 → 刷新（端到端出图）

## M3 — 服务端完善
- [ ] 设备注册/心跳/状态上报
- [ ] 内容管理 API + 简单 Web 上传页
- [ ] OTA：固件上传 + 设备升级流程

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
