# FramedPhoto 服务端 API

基础地址：`http://<host>:8010`；Web 管理台：`GET /`（上传、内容库、设备、OTA）。

## 设备端接口（固件调用）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/images/content` | 内容清单（`images[0]` 为当前应显示图片，附 `url`） |
| GET | `/api/images/{id}/raw` | 下载 FPS6 设备格式（1200x1600，4bit 双 IC 布局） |
| POST | `/api/devices/register` | 设备注册：`{"device_id" 或 "mac", "name"}` |
| POST | `/api/devices/{id}/heartbeat` | 心跳：`{"firmware_version","battery","current_image","ip"}` |
| GET | `/api/ota/manifest?current_version=` | OTA 轮询：返回 `detail`（新固件）或 `null` |
| GET | `/api/ota/download` | 下载最新固件 bin |

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
| GET | `/api/images/{id}/preview` | PNG 预览 |
| DELETE | `/api/images/{id}` | 删除图片 |
| GET | `/api/devices` | 设备列表（在线状态按 last_seen 判断） |
| POST | `/api/ota/upload` | 发布固件：multipart `file` + `version`（校验 0xE9 魔数） |
| GET | `/api/ota/versions` | 最新固件信息 |
| GET | `/api/settings` | 当前生效配置（时段编排 / 天气 / 自由模块）+ 状态（IP 定位城市、当前时段、今日自由模块） |
| PUT | `/api/settings` | 保存配置（即时生效，写入 `runtime_config.json`）；天气相关设置变化会自动清理天气缓存 |
| GET | `/api/weather/lookup?location=宝山` | 中文城市名 → 和风 location 列表（管理台城市输入框保存时自动调用） |

### 天气城市设置

管理台「城市」输入框保存时，中文城市名会自动解析为和风 location（城市 ID / 经纬度），也支持直接填城市 ID、经纬度或拼音。解析逻辑：前端内置常用城市表 → `/api/weather/lookup`（GeoAPI + 常见城市兜底）。保存后天气卡片按新城市渲染（缓存文件名带 location 指纹，改城市立即失效）。留空则回退 IP 自动定位。

## 数据存储

- SQLite：`services/framedphoto.db`（表：devices / images / firmware）
- 图片文件：`services/uploads/`；固件：`services/ota/`
- 均通过 `.env` 可配置（`DB_PATH` / `UPLOAD_DIR` / `OTA_DIR`）

## 固件对接（FPS6 格式）

见 [docs/protocol/fps6-format.md](fps6-format.md)。
