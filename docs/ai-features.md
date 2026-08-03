# AI 照片分析与每日精选

FramedPhoto 的「AI 回忆相框」能力：分析照片库 → 评分 → 每日自动选出
「历史上的今天」最值得看的照片，带文案渲染到相框。

## 1. 分析照片库

```bash
# 1) 配置 VLM（可选，不配则用启发式评分）
#    services/.env:
#      VLM_ENABLED=true
#      VLM_API_URL=http://127.0.0.1:1234/v1/chat/completions   # LM Studio / 云端
#      VLM_API_KEY=
#      VLM_MODEL=qwen3-vl-32b-instruct

# 2) 扫描并分析（并发 4，断点续跑）
python tools/analyze_photos.py /path/to/photos [-j 4] [--no-vlm]
```

每张照片产出：类型 / 回忆度 / 美观度 / 一句话文案 / 拍摄时间（EXIF）/ GPS，
存入 SQLite `photo_scores` 表。VLM 不可用时自动降级启发式评分（清晰度、色彩、
亮度），保证离线可跑。

## 2. 每日精选（自动）

服务端按"今天月-日"匹配历年同一天的照片（须有真实 EXIF 拍摄时间），
回忆度 ≥ 阈值（`DAILY_MIN_SCORE`），加权随机选一张（近期用过的降权防重复），
渲染成带文案的 FPS6：

- 底部黑色渐变带 + 日期小字 + 文案白字（中文，6 色量化前绘制）
- 输出 `services/daily/daily.fps6`，设备直接拉取显示
- 无历史上的今天候选时，降级为全局高分未用照片

## 3. 内容优先级

设备轮询 `GET /api/images/content`：

1. **手动上传**的最新图片（用户主动上传 = 想看的）
2. 无手动内容时 → **每日精选**

## 4. 设备端行为

- 每次显示后**深度休眠**（`idf.py menuconfig` → `FRAMEDPHOTO_DEEP_SLEEP_HOURS`，
  默认 24h，0 = 禁用调试）
- RTC 定时器唤醒 → 检查内容 → 无变化不刷屏直接再睡（上次内容存 NVS）
- 长续航设计：E Ink 静态零功耗 + 深度休眠

## 说明

- 本能力**借鉴了开源项目 InkTime 的产品思路**（AI 评分、历史上的今天、
  每日自动换片），但全部为 FramedPhoto 原创实现：不同技术栈（FastAPI /
  ESP-IDF）、不同渲染管线（FPS6 4bit 双 IC）、不同代码。
- VLM 提示词在 `services/app/analyzer.py` 的 `SYSTEM_PROMPT`，可自行调整
  评分标准和文案风格。
