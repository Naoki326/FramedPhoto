# 免费生图服务：每日自动生图相框调研

## 已核实结论

核查系统时间：2026-11-21 23:00 CST。以下为本次实际获取的官方文字，不沿用历史免费政策；未登录、未读取密钥、未调用生成接口。首选 **Cloudflare Workers AI 的 FLUX.1 schnell**，有每日恢复的免费计算额度，但需要接口适配。不能把平台所有模型都视为免费。

| 服务 | 当前核实结果 | 每日自动相框建议 |
|---|---|---|
| Cloudflare Workers AI | Free / Paid 均有每日 10,000 Neurons；00:00 UTC 重置。FLUX.1 schnell 列有 Neurons 计费，可在额度内使用 | 首选；额度与其他 Workers AI 使用共享，不是固定张数 |
| Hugging Face Inference Providers | 当前 Free Users：**None**，需购买 credits 或升级 PRO；PRO 每月 $2 是付费订阅所含额度 | 不能当作免费常驻生图 API |
| Google Gemini API | 核实的 Gemini 图片模型 Free Tier 为 Not available；Imagen 官方文档称已关闭 | 不推荐作免费生图后端；文本免费不等于图片输出免费 |
| Pollinations | API 需要 key，使用余额；未核实任何人人可得的固定每日免费积分 | 不推荐按“免 key、无限免费”接入 |
| Bing Image Creator | 微软个人账户网页免费；每日 15 次快速生成，用尽转标准速度 | 可手动生图；本次页面没有可供相框调用的公开 API 证据 |

## 1. Cloudflare Workers AI（首选）

官方定价：https://developers.cloudflare.com/workers-ai/platform/pricing/ （HTTP 200）

实际获取文字：

> “Our free allocation allows anyone to use a total of 10,000 Neurons per day at no charge.”
>
> “To use more than 10,000 Neurons per day, you need to sign up for the Workers Paid plan.”
>
> “All limits reset daily at 00:00 UTC.”

定价表明确列出 Workers Free 每日 10,000 Neurons，超出需升级；Paid 超过免费额度按 $0.011 / 1,000 Neurons 计费。免费计划适合防止自动任务超额付费，但仍须处理额度耗尽和失败。

本次明确推荐模型：`@cf/black-forest-labs/flux-1-schnell`。定价表文字：

> “4.80 neurons per 512x512 tile”
>
> “9.60 neurons per step”

不能直接把 10,000 Neurons 换成保证的每日张数；尺寸、步数和其他模型使用都会影响消耗。每天一张属于优先验证场景，而非本次实测成功承诺。

模型文档：https://developers.cloudflare.com/workers-ai/models/flux-1-schnell/ （HTTP 200）

> “Model ID @cf/black-forest-labs/flux-1-schnell”
>
> “The number of diffusion steps; higher values can improve quality but take longer. Default is 4”
>
> “The generated image in Base64 format.”

官方示例使用账户级 `/client/v4/accounts/.../ai/run/@cf/black-forest-labs/flux-1-schnell` 和 Bearer token；模型示例读取 `response.image`，不是 OpenAI 的 `data[0].b64_json`。

**模型边界：**定价页同时明确 “Some models require a paid billing method”，列举 kimi、glm、部分 deepseek、inkling 模型，说明免费额度不是所有模型的无条件免费通行证。本次不把其他图片模型一并列为已确认免费。`stable-diffusion-xl-base-1.0`、`lucid-origin`、`flux-2-klein-9b` 模型页面也获取到 HTTP 200，但未完成逐模型免费资格交叉核验，不据此推荐；当前定价表没有为本次读取的旧 SDXL 模型提供可直接引用的额度依据。

## 2. Hugging Face Inference Providers：免费账户额度已不能按旧印象说

官方定价：https://huggingface.co/docs/inference-providers/pricing （HTTP 200）

实际获取文字：

> “Free users can use Inference Providers by purchasing credits, or by upgrading to PRO”
>
> “Free Users None - yes (credits purchase required)”
>
> “PRO Users $2.00 All Hugging Face compute services”

因此本次不引用历史的免费账户 $0.10/月说法。PRO 的每月 $2 属于付费订阅附带计算额度，不是零成本常驻额度。

图片能力依赖具体模型与 provider；不能把模型可下载、Spaces 演示、文字模型 API 可用当作免费图片 API。官方文本还说：

> “As of July 2025, hf-inference focuses mostly on CPU inference”

图像任务官方文档已取得 HTTP 200：https://huggingface.co/docs/inference-providers/tasks/text-to-image 。本次没有实际执行 provider 的图片生成，不保证指定模型可用或 OpenAI 图片协议兼容。

## 3. Google Gemini / Imagen：文本免费不等于生图免费

官方定价：https://ai.google.dev/gemini-api/docs/pricing （HTTP 200）

实际获取到的 `Gemini 2.5 Flash Image` Standard 表：

> “Input price / Not available / $0.30 (text / image)”
>
> “Output price / Not available / $0.039 per image”

其中 `Not available` 位于 **Free Tier** 列。`Gemini 3.1 Flash Lite Image` 的 Standard 表同样写 Free Tier 输入、输出均 `Not available`，付费图片输出示例为每 1K 分辨率图 $0.0336。不能因为 Gemini 有文本免费层或页面提供 “Try it in Google AI Studio” 就推断图片 API 免费。

Imagen 官方文档：https://ai.google.dev/gemini-api/docs/imagen （HTTP 200）

> “Imagen models are shut down. Use Nano Banana for image generation.”
>
> “It is now shut down and no longer available in the Gemini API.”
>
> “Nano Banana returns content parts containing image data instead of a specific image response object.”

这是本次获取页面的当前文字；不能继续按历史 Imagen 免费试用宣传。此结论限定 Gemini API，不扩展至 Vertex AI 等不同产品。

## 4. Pollinations：必须区分免费宣传与当前余额体系

官方入口：https://enter.pollinations.ai/api/docs （HTTP 200，但正文是动态文档外壳，初始可读文字不足）。随后按页面给出的官方链接取得：

- https://gen.pollinations.ai/llms.txt （HTTP 200）
- https://gen.pollinations.ai/docs/llm.txt （HTTP 200）
- https://gen.pollinations.ai/image/models （HTTP 200，仅读公共模型目录，不生成图片）

实际获取文字：

> “API calls use a Pollinations key.”
>
> “Amounts are in US dollars … Fields and values named pollen … are US dollars too.”
>
> “402 … Balance too low, key budget exhausted, or model needs the paid balance”

文档区分 Quest balance 与 paid balance，但本次**未核实人人自动获得的每日免费数额、恢复规则、资格要求**，不能称为常驻免费 API。目录中 `tongyi-mai/z-image-turbo` 的 `pricing` 实际为 `{"currency":"pollen","completionImageTokens":"0.004"}`，`paid_only:false` 不能解读为价格为零。

官方文档说明 `/v1/images/generations`，并写：

> “OpenAI-compatible responses default to b64_json; set response_format: url for a stored media URL.”

此段位于 community image models 说明中，不能保证所有模型均兼容本项目尺寸。若以后接入，应逐模型核验请求参数、价格和返回结构，不沿用旧免 key 图片 URL。

## 5. Bing Image Creator：网页免费，不作为自动 API 推荐

微软官方：https://www.microsoft.com/en-us/bing/features/bing-image-creator （HTTP 200）

实际获取文字：

> “free for everyone with a Microsoft Account (MSA)”
>
> “When you've used up your 15 free fast creations per day … you'll be switched to … standard creation speed automatically.”

页面同时说明 Microsoft Entra ID 工作/学校账户不适用。网页免费不代表存在可供相框后台自动调用的免费公开 API；本次没有核实到该类 API，不建议抓网页、模拟登录或依赖非官方封装。

## 项目接入判断（仅短读源码，未修改）

- `services/app/llm.py:89–121`：图片端点分 `openai` / `signed_async`；OpenAI 配置为 URL、key、model，三项必须齐全。
- `services/app/free_module.py:327–348` 的 `_generate_cartoon`：POST 配置 URL，Bearer 认证；固定发送 `model`、`prompt`、`n:1`、`size:"1536x1024"`、`response_format:"b64_json"`，只读取 **`data[0].b64_json`** 并解码。
- 因而“可配置 URL”不等于支持任意生图接口，甚至 OpenAI 风格仅返回 `data[0].url` 也不兼容。
- Cloudflare FLUX 需要适配账户路径、请求参数、返回图片字段；不能只替换 URL。
- Gemini 返回 content parts 图片数据，需要专门适配；本次也没核实到免费图片额度。
- HF 需按 provider 适配，不能把其文字 OpenAI 兼容当成图片兼容。
- Pollinations 的 OpenAI 图片路径最接近现有形态，但须确认具体模型支持固定尺寸和 b64 返回，且余额并非已证实免费。

## 免费含义与核查边界

1. **周期恢复免费额度**：本次确认 Cloudflare 每日 Neurons；Bing 是网页每日快速额度，不是后台 API。
2. **付费订阅附带额度**：HF PRO 月度 $2 不属于零成本。
3. **一次性试用/活动赠金**：本次没有核实可推荐的具体额度；不得当作长期每日相框预算。
4. **自托管开源模型**：模型权重开放不等于免费云 GPU；硬件、电费或 GPU 租赁需另算，本次不推荐为免费托管服务。
5. 信用卡、地区可用性及中国网络可达性未完成证据核实，不作无需信用卡或某地区必可用的保证。未核实 SiliconFlow FLUX 免费政策，不列为免费推荐。

所有网络获取均使用 `curl -L --max-time 15 --connect-timeout 5`，每次 bash 设置 timeout；无生成、无付费调用、无账户余额查询。上述页面均 HTTP 200；Pollinations 初始动态页面信息不足已注明并使用其官方纯文本补充。未做真实生成、质量或稳定性测试。仅新增本文件，不触碰工作区已有产品改动。
