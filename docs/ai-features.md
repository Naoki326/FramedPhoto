# AI 照片分析与每日精选

FramedPhoto 的「AI 回忆相框」能力：分析照片库 → 评分 → 每日自动选出
「历史上的今天」最值得看的照片，带文案渲染到相框。

## 1. 配置 VLM（两种方式：管理台或 `.env`）

**方式一（推荐）：管理台「🧠 AI」页** —— 选 provider、填 URL / key / 模型，保存即时生效，无需重启。密钥只显示掩码（不编辑就保留原值，填 `-` 置空）；页内「🔌 测试连接」会发一张小图验证端点/密钥/代理真的通。

**方式二：`services/.env`**（从 `services/.env.example` 复制）：

```ini
# A) OpenAI 兼容 + Responses API（gpt-5.x 类新模型，需经代理出口在支持区域）
VLM_PROVIDER=openai
VLM_API_URL=https://<你的中转或云厂商>/v1
VLM_API_MODE=responses        # gpt-5.x 等新模型必须用 /v1/responses
VLM_API_KEY=sk-xxx
VLM_MODEL=<视觉模型名>
VLM_PROXY=http://127.0.0.1:7897  # OpenAI 模型有区域限制，需代理出口在
                                 # 支持区域（美/日/新等；香港/大陆不可用）

# B) Anthropic（Claude 视觉）—— 有 key 时 auto 模式优先用
# VLM_PROVIDER=anthropic
# ANTHROPIC_API_KEY=sk-ant-xxx
# ANTHROPIC_MODEL=claude-sonnet-4-20250514

# C) OpenAI 兼容接口（本地模型服务 / 国内云，无需代理）
# VLM_PROVIDER=openai
# VLM_API_MODE=chat
# VLM_API_URL=http://127.0.0.1:1234/v1/chat/completions
# VLM_MODEL=<视觉模型名>

# D) 关闭 AI，纯启发式评分
# VLM_PROVIDER=disabled
```

> 视觉模型选型经验：**deepseek / glm / qwen*-max 部分型号不支持视觉输入**，
> 不能用；选型时先确认模型真的接受图片输入，或直接用管理台的「测试连接」验证。

`VLM_API_MODE`：`chat`（chat/completions）或 `responses`（/v1/responses）。
GPT-5.x 等新模型仅支持 responses；URL 填 base（`.../v1`）或完整
chat/completions 路径均可，客户端会自动规范。

VLM 调用失败（网络 / 限流 / 无 key / 区域限制）时自动降级启发式，不中断分析。

## 1.1 文本 LLM 与文生图（自由模块 / 日历寄语 / 天气设计）

同样在管理台「🧠 AI」页配置，**按接口形态描述、不绑厂家**（换厂家 = 换地址与 key）：

| 用途 | 配置 |
| --- | --- |
| 文本 LLM（自由模块文案 / 日历寄语 / 天气卡片设计） | 接口类型（OpenAI 兼容 / Anthropic 兼容）+ base URL + key + 模型 |
| 文生图（自由模块插画） | 接口类型 `openai`（URL+KEY+MODEL）、`signed_async`（AK/SK）或 `cloudflare`（Account ID+API token）+ 出图比例 |

对应 `.env` 键为 `TEXT_*` 与 `IMAGE_*`（见 `services/.env.example`）。历史的
`ZHIPU_*` / `JIMENG_*` / `IMAGEGEN_*` / `QWEATHER_*` 仍能读到（新键优先），
可逐步替换。OpenAI 与签名式链路可同时配置并互为失败回退。选择 Cloudflare 时仅请求
Cloudflare，失败降级纯文字卡片，不调用已有的其他付费生图接口。

### Cloudflare Workers AI 设置

1. 在 Cloudflare 控制台的 Workers AI → **Use REST API**，创建专用 API token，保存 Account ID。
2. 打开相框管理台 **🧠 AI → 文生图**，接口类型选 **Cloudflare Workers AI（FLUX.1 schnell）**。
3. 填写 32 位十六进制 **Account ID** 和 **API token**，点击 **保存 AI 配置**。
   token 输入框使用密码样式，保存后只显示掩码；留空保留旧值，填 `-` 清除。
4. 点击 **检查链路**仅验证本地配置完整性，**不验证远端权限、额度或真实生图能力**，不消耗生图额度。
5. 需要实际测试时，在 **当前显示 → 重新生成今日自由模块** 生成一张图；这会消耗账户额度并更新当前自由模块卡片，不是隔离预览。

固定使用 `@cf/black-forest-labs/flux-1-schnell`，无需提供 URL 或模型名。该接口使用模型默认尺寸，
不支持本项目的比例参数，选择 Cloudflare 后比例控件禁用；相框仍会按原流程裁剪适配。账户每日免费
10,000 Neurons 与其他 Workers AI 用量共享，不是无限免费或保证张数；免费计划、账单与
用量请在 Cloudflare 控制台自行确认。相框不创建云端资源、不升级套餐，也不自动购买额度。
接口错误、限流或余额不足走现有文字卡片降级。环境配置见 `CLOUDFLARE_ACCOUNT_ID` 与
`CLOUDFLARE_API_TOKEN`；不要把 token 发到聊天、截图或提交进 git。

### 童话／历史题库轮换

仅当模块 prompt 与内置「童话故事」或「历史故事」默认文本**完全一致**时启用
（模块名可改，插画风格仍生效）；编辑 prompt 后回到原有开放生成，不强改自定义要求。
`services/app/story_catalog.py` 目前每类 **20 条**，含稳定故事 ID、标题别名、原创梗概、
分类与作品／文献出处；引用未逐条在线核验，史书记述、传说、小说演义分别标注，
不把后世故事当作已证史实，也未完成逐条专业史料审校。

程序先选题，再让 LLM 按梗概写画；ID／标题不符或调用失败直接使用题库梗概和画面模板，
文本最多请求一次（30 秒超时），不反复重试。ID、标题和字段校验**不能证明正文语义或
画面完全忠于事实卡**，仍需人工抽检。历史模块保留出处中的不确定性提示。

持久化按成功生成次数未用优先，正常完整周期为每类 20 次，冷却为最近 19 次同类成功生成，
分类在候选允许时轮换；不是 20 个自然日，更不承诺永久不重复。旧标题按已知别名尽力归并，
未知标题保留但不声称完成故事识别；迁移／扩库导致约束冲突时可有日志地放宽较长冷却，
不重复紧邻故事。标题历史仍按旧模块名查找，旧模块已经改名且非默认名时无法自动推断归属。

历史原子替换；线程锁及 POSIX `flock` 串行保护选择到落盘（同一历史文件、本地支持锁的
文件系统，多机／不可靠网络文件系统不保证）。槽位包含起点和模块配置摘要，同槽位及重启
复用不重复消耗，强制刷新仍锁定该槽位原故事；预览生成也计入**成功生成历史**，不是设备
实际展示。先保存卡片再记历史；缓存命中核对历史，忽略崩溃遗留的未记账帧。历史损坏或
写失败拒绝新生成，不当作空历史放行；记录选题、降级及持久化错误日志。历史保留完整生成
记录，尚未做压缩；内容库入库与历史并非数据库事务，故障时可能留下未被返回的归档卡。

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
   - 天气时段 → 天气卡片（天气 API 未配置或渲染失败时回退照片逻辑）
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
