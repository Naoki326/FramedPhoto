# FramedPhoto 服务端 API

基础地址：`http://<host>:8010`；Web 管理台：`GET /`（上传、内容库、设备、OTA）。

## 设备端接口（固件调用）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/images/content` | 内容清单（`images[0]` 为当前应显示图片，附 `url`） |
| GET | `/api/images/{id}/raw` | **正式设备下载契约**：下载 FPS6 设备格式（1200x1600，4bit 双 IC 布局），见下文 |
| POST | `/api/devices/register` | 设备注册：`{"device_id" 或 "mac", "name"}` |
| POST | `/api/devices/{id}/heartbeat` | 心跳：`{"firmware_version","battery","current_image","ip"}` |
| GET | `/api/ota/manifest?current_version=` | OTA 轮询：返回 `detail`（新固件）或 `null` |
| GET | `/api/ota/download` | 下载最新固件 bin |

### 设备下载契约：`GET /api/images/{id}/raw`

这是唯一的设备下载端点（ADR-0002）：内容清单与「推送到显示」的 `url`
一律指向它。服务端按 id 前缀分发到六种内容源——`display-`（置顶显示）、
`daily-`（每日精选）、`weather-`（天气卡片）、`calendar-`（日历卡片）、
`free-`（自由模块；`news-`
为旧称前缀，旧固件按 id 拼 URL 的兑底仍命中同一内容）；无前缀 id 为
内容库图片。生成源的 id 是渲染字节的内容指纹（变更检测用，不是查找
键）：同一时段内指纹不变即内容未变，设备不必重复下载；服务端一律
返回当前内容，不按指纹寻址。历史的 `/api/images/{source}/raw|preview`
固定路由已移除。

内容清单响应示例：

```json
{
  "images": [
    {
      "id": "a04f8e00391d",
      "filename": "frame.png",
      "width": 1200,
      "height": 1600,
      "created_at": 1785749298.15,
      "url": "/api/images/a04f8e00391d/raw"
    }
  ]
}
```

## 管理接口（Web/脚本调用）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/images/upload` | 上传图片（multipart `file`，可选 `dither`），自动转换 |
| GET | `/api/images` | 图片列表 |
| GET | `/api/images/{id}/preview` | 内容预览（五源共用：原图缩放 ~800px JPEG，缺原图 404，ADR-0001） |
| DELETE | `/api/images/{id}` | 删除图片 |
| GET | `/api/devices` | 设备列表（在线状态按 last_seen 判断） |
| POST | `/api/ota/upload` | 发布固件：multipart `file` + `version`（校验 0xE9 魔数） |
| GET | `/api/ota/versions` | 最新固件信息 |
| GET | `/api/settings` | 当前生效配置（时段编排 / 天气 / 自由模块）+ 状态（IP 定位城市、当前时段、今日自由模块） |
| PUT | `/api/settings` | 保存配置（即时生效，写入 `runtime_config.json`）；天气相关设置变化会自动清理天气缓存 |
| GET | `/api/weather/lookup?location=宝山` | 中文城市名 → 天气 API location 列表（管理台城市输入框保存时自动调用） |
| GET | `/api/calibration` | 校准状态（六色采样值 / 是否已校准 / 浓彩偏置 chroma_bias） |
| POST | `/api/calibration/generate` | 生成校准图并直推设备（nibble 直写，绕过量化） |
| GET | `/api/calibration/chart` | 校准图 PNG 预览（横放视角） |
| POST | `/api/calibration/photo` | 上传校准照片 → 采样六色 → 写 calibrated.json 热加载 |
| GET | `/api/calibration/rainbow` | 彩虹效果图 PNG 预览（正常量化链路） |
| POST | `/api/calibration/rainbow/push` | 生成彩虹效果图并直推设备（含浓彩偏置） |
| PUT | `/api/calibration/chroma-bias` | 设置浓彩偏置 `{"value": 0..4}`（0=忠实混色，ADR-0004） |
| DELETE | `/api/calibration` | 清除校准，恢复默认占位色 |
| GET | `/api/analysis/scores?category=X&sort=memory\|shot_at` | 照片评分列表（回忆度/拍摄时间排序），可选按分类过滤；返回 `summary`（分类汇总，供管理台 tab 排序） |
| POST | `/api/analysis/upload` | 上传照片到 photo 文件夹（multipart `folder` + `files` + 可选 `paths`；folder 缺省/空 = 根目录即未分类；目标文件夹即分类，不存在自动创建，同名跳过；自动推回 NAS） |
| GET | `/api/analysis/folders` | photo 文件夹列表（本地镜像为准，缺失时回退 NAS 实际列表） |
| POST | `/api/analysis/folders` | 在 NAS photo 下新建空文件夹（即新分类；名字不可叫「未分类」） |
| PATCH | `/api/analysis/folders/{name}` | 重命名文件夹：NAS mv → 镜像 mv + 迁移评分记录（path 前缀与分类）+ 联动时段绑定与手动精选（ADR-0007） |
| DELETE | `/api/analysis/folders/{name}` | 删除空文件夹（NAS rmdir + 本地，连带清理残留评分记录与时段绑定）；非空 409 |
| POST | `/api/sync/start` | 手动触发一次 NAS → 本地增量同步（后台运行） |
| GET | `/api/sync/status` | 同步进度（running/done/error/stale/idle） |
| GET | `/api/ai` | AI 接入配置（生效值 + 密钥掩码 `sk-1…cd34` 与 `*_set` 就绪位）+ 各组状态（`vlm_ready`/`text_ready`/`image_ready`/`weather_ready`/`nas_ready`） |
| PUT | `/api/ai` | 保存 AI 接入配置（写入 `runtime_config.json`，即时生效）。密钥字段：留空或回传掩码 = 保持原值，`"-"` = 置空（不回退 `.env`）；天气 key/host 变化自动清天气缓存 |
| POST | `/api/ai/test` | 连通性测试 `{"target": "vlm"\|"text"\|"image"}`：前两者发真实最小请求，`image` 只检查链路就绪性（不实际出图，按次计费） |
| GET | `/api/ai/calendar-quote?force=false` | 当日家庭寄语（`force=true` 忽略缓存重生成，供 AI 页调试） |

### AI 接入配置（管理台「AI 模型」页）

LLM 与文生图接入按**接口形态**配置，不绑定厂家（换厂家 = 换地址与 key）：

| 配置面 | provider | 字段 |
| --- | --- | --- |
| 照片分析 VLM | `auto` / `openai`（含 `chat`、`responses` 两种端点） / `anthropic` / `disabled` | `vlm_api_url` `vlm_api_key` `vlm_model` `vlm_timeout` `vlm_proxy`，以及 `anthropic_*` |
| 文本 LLM | `openai`（chat/completions） / `anthropic`（/v1/messages） | `text_base_url` `text_api_key` `text_model` |
| 文生图 | `openai` / `signed_async` / `cloudflare`（Workers AI FLUX.1 schnell） | 前两者配置不变；Cloudflare 使用 `cloudflare_account_id`（32 位十六进制）及 `cloudflare_api_token`（密钥掩码）；Cloudflare 使用模型默认尺寸，忽略 `image_ratio`。`cloudflare_ready` 仅表示配置齐全，不代表远端鉴权或额度通过；Cloudflare 失败不回退其他生图接口 |

读取优先级统一为 `runtime_config.json` > `services/.env` > 默认值，保存即时生效，无需重启。签名式接口的端点/区域/请求 Action 由适配层 `app/image_signed.py` 持有，换厂家只改该文件的模块常量。

**旧厂商名键兼容**：`ZHIPU_*` / `JIMENG_*` / `IMAGEGEN_*` / `QWEATHER_*` 仍能读到（新键优先，`.env` 与历史 `runtime_config.json` 都支持），因此改名不影响已有部署。

### 天气城市设置

管理台「城市」输入框保存时，中文城市名会自动解析为天气 API location（城市 ID / 经纬度），也支持直接填城市 ID、经纬度或拼音。解析逻辑：前端内置常用城市表 → `/api/weather/lookup`（GeoAPI + 常见城市兜底）。保存后天气卡片按新城市渲染（缓存文件名带 location 指纹，改城市立即失效）。留空则回退 IP 自动定位。

## 数据存储

- SQLite：`services/framedphoto.db`（表：devices / images / firmware）
- 图片文件：`services/uploads/`；固件：`services/ota/`
- 均通过 `.env` 可配置（`DB_PATH` / `UPLOAD_DIR` / `OTA_DIR`）

## 固件对接（FPS6 格式）

见 [docs/protocol/fps6-format.md](fps6-format.md)。
