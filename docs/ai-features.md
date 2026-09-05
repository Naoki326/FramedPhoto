# AI 照片分析与每日精选

FramedPhoto 的「AI 回忆相框」能力：分析照片库 → 评分 → 每日自动选出
「历史上的今天」最值得看的照片，带文案渲染到相框。

## 1. 配置 VLM（三种方式，任选其一）

`services/.env`（从 `services/.env.example` 复制）：

```ini
# A) OpenCode Go + gpt-5.6-luna（Responses API + 代理）
VLM_PROVIDER=openai
VLM_API_URL=https://opencode.ai/zen/go/v1
VLM_API_MODE=responses        # gpt-5.x 等新模型必须用 /v1/responses
VLM_API_KEY=sk-xxx
VLM_MODEL=gpt-5.6-luna
VLM_PROXY=http://127.0.0.1:7897  # OpenAI 模型有区域限制，需代理出口在
                                 # 支持区域（美/日/新等；香港/大陆不可用）

# B) Anthropic（Claude 视觉）—— 有 key 时 auto 模式优先用
# VLM_PROVIDER=anthropic
# ANTHROPIC_API_KEY=sk-ant-xxx
# ANTHROPIC_MODEL=claude-sonnet-4-20250514

# C) OpenAI 兼容接口（LM Studio / 国内云，无需代理）
# VLM_PROVIDER=openai
# VLM_API_MODE=chat
# VLM_API_URL=http://127.0.0.1:1234/v1/chat/completions
# VLM_MODEL=qwen3.8-max

# D) 关闭 AI，纯启发式评分
# VLM_PROVIDER=disabled
```

> 视觉模型选型经验：**deepseek / glm / qwen3.7-max 不支持视觉输入**，
> 不能用；OpenCode Go 上 **qwen3.8-max、minimax-m3** 是验证可用的视觉备选。

`VLM_API_MODE`：`chat`（chat/completions）或 `responses`（/v1/responses）。
GPT-5.x 等新模型仅支持 responses；URL 填 base（`.../v1`）或完整
chat/completions 路径均可，客户端会自动规范。

VLM 调用失败（网络 / 限流 / 无 key / 区域限制）时自动降级启发式，不中断分析。

## 2. 分析照片库

```bash
# 扫描并分析（并发 4，断点续跑）
python tools/analyze_photos.py /path/to/photos [-j 4]
```

每张照片产出：类型 / 回忆度 / 美观度 / 一句话文案 / 拍摄时间（EXIF）/ GPS，
存入 SQLite `photo_scores` 表。VLM 不可用时自动降级启发式评分（清晰度、色彩、
亮度），保证离线可跑。

## 3. 每日精选（自动）

服务端按"今天月-日"匹配历年同一天的照片（须有真实 EXIF 拍摄时间），
回忆度 ≥ 阈值（`DAILY_MIN_SCORE`），加权随机选一张（近期用过的降权防重复），
渲染成带文案的 FPS6：

- 底部黑色渐变带 + 日期小字 + 文案白字（中文，6 色量化前绘制）
- 输出 `services/daily/daily.fps6`，设备直接拉取显示
- 无历史上的今天候选时，降级为全局高分未用照片

## 4. 内容优先级

设备轮询 `GET /api/images/content`，按以下顺序决定当前应显示内容：

1. **置顶显示**：一次性上传切换，优先于一切时段内容
2. 无置顶时按**时段编排**（见 `app/slots.py`）：
   - 天气时段 → 天气卡片（和风未配置或渲染失败时回退照片逻辑）
   - 自由模块时段 → 自由模块卡片（LLM / 文生图依赖不可用时回退照片逻辑）
   - 照片时段 → 按以下三级：
     1. 内容库「推送到显示」的图（当日有效）
     2. 手动精选（管理台「设为今日精选」）
     3. 每日精选（自动选片）

## 5. 设备端行为

- 每次显示后**深度休眠**（`idf.py menuconfig` → `FRAMEDPHOTO_DEEP_SLEEP_MIN`，
  默认 30min，0 = 禁用调试）
- **唤醒对齐**：心跳响应的 server_time 校准设备墙钟，默认睡到下一个本地
  半点边界加一分钟（每个整点的 ：01 / :31）再 RTC 唤醒（`FRAMEDPHOTO_ALIGN_WAKE`，
  时区 `FRAMEDPHOTO_TIMEZONE` 默认北京时间）；时钟从未校准过时退回固定间隔
- RTC 定时器唤醒 → 检查内容 → 无变化不刷屏直接再睡（上次内容存 NVS）
- **刷新按钮**：按下立即拉取并刷新（`FRAMEDPHOTO_BUTTON_GPIO`，默认 GPIO 0 =
  板上 BOOT 键，零接线）。深度休眠中按下走 EXT1 唤醒，强制刷新一次；
  轮询等待中按下走中断，立即刷新。上传新图后按一下即可马上看到。
- 长续航设计：E Ink 静态零功耗 + 深度休眠

## 说明

- 本能力**借鉴了开源项目 InkTime 的产品思路**（AI 评分、历史上的今天、
  每日自动换片），但全部为 FramedPhoto 原创实现：不同技术栈（FastAPI /
  ESP-IDF）、不同渲染管线（FPS6 4bit 双 IC）、不同代码。
- VLM 提示词在 `services/app/analyzer.py` 的 `SYSTEM_PROMPT`，可自行调整
  评分标准和文案风格。
