# 路线图

## M0 — 环境与骨架（完成 ✅）
- [x] Git 仓库初始化、目录结构
- [x] ESP-IDF v6.0.x 环境确认，固件骨架可编译
- [x] 服务端 FastAPI 骨架
- [x] 官方资料下载并归档 → `docs/vendor/gdeb0709e01/`

## M1 — 点亮屏幕（驱动完成 ✅，待真机验证）
- [x] 官方 ESP32 示例源码解析：双 IC 结构、4bit/像素、init/刷新序列
- [x] 驱动移植进 `firmware/components/gdey_epd/`
- [x] 目标芯片 **ESP32-S3 + 16MB flash**，双 OTA + storage 分区
- [x] 演示模式：纯色 / 6 色横条 / 棋盘格（600B RAM 流式）
- [x] 服务端 FPS6 格式对齐，协议文档 `docs/protocol/fps6-format.md`
- [ ] **真机验证**：烧录后屏幕依次显示 白屏 → 6 色横条 → 棋盘格

## M2 — 联网取图（代码完成 ✅，待真机联调）
- [x] WiFi（NVS 凭据 + menuconfig）
- [x] content_client：内容清单（cJSON）+ FPS6 下载到 storage 分区
- [x] 流式渲染（两阶段双 IC），设备模拟器 + 集成测试验证一致
- [ ] 真机联调：WiFi 配置 → 服务端上传 → 设备显示

## M3 — 服务端基础（完成 ✅）
- [x] SQLite 持久化（devices / images / firmware）
- [x] 设备注册 / 心跳 / 状态查询
- [x] 内容管理 API + Web 管理台（上传/内容库/设备/OTA）
- [x] OTA：固件上传（0xE9 校验 + sha256）+ manifest 轮询 + 下载

## M4 — AI 回忆相框（完成 ✅，参考 InkTime 思路、代码原创）
- [x] `app/analyzer.py`：照片 AI 分析（OpenAI 兼容 VLM：描述/类型/回忆度/美观度/文案）
      + **无 VLM 时启发式评分降级**（清晰度/色彩/亮度）
- [x] EXIF 提取（拍摄时间/GPS），区分 exif/file 来源防止"历史上的今天"误判
- [x] `tools/analyze_photos.py`：批量分析 CLI（并发、断点续跑）
- [x] `app/daily.py`：历史上的今天选片（评分加权 + 防重复）+ 每日精选渲染（带文案）
- [x] content 优先级：手动上传 > 每日精选
- [x] 文案渲染：`prepare_image_with_caption`（底部渐变 + 中文白字，6 色量化前绘制）
- [x] 设备端深度休眠（RTC 定时器唤醒 + NVS 记忆上次内容，无变化不刷屏）
- [x] WebUI：今日精选展示 + 照片评分列表

## 当前进度（2026-08）

- 服务端 105+ 单测全绿，固件编译通过（esp32s3，958KB）；launchd 服务运行在 :8010
- **NAS 首次全量同步已完成**（rsync over SSH，373 文件 / 约 3.4GB），**全量 AI 照片分析已完成**（392 张全部入库评分）
- 天气卡四风格 + 城市自定义、管理台 24h 时序编排、屏幕校准工具链已上线
- **真机在线**：ESP32-S3（0.1.8）已接入服务端，WiFi 拉取内容 → 自由模块卡片正常显示，深度休眠轮询循环运行中

## M5 — 打磨（真机阶段）
- [x] 全量 AI 照片分析（392 张，`photo_scores` 全部入库）
- [x] 真机联调：显示 / WiFi / 深度休眠全流程（设备在线轮询中）
- [x] 设备端 OTA 客户端（esp_ota 分区切换 + sha256 校验 + bootloader 回滚）
- [ ] 断网重连 / 异常恢复
- [ ] 部署：docker-compose 一键起服务

## 资料清单（docs/vendor/gdeb0709e01/）

| 文件 | 说明 |
| --- | --- |
| GDEB0709E01_规格书.pdf | 官方数据手册（23 页） |
| E6设计须知_CN.pdf | Spectra 6 面板设计注意事项 |
| 固件烧录方式.pdf | 配套板固件烧录方法 |
| esp32_example/ | 官方 ESP-IDF 示例源码 |
