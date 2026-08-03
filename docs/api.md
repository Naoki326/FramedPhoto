# FramedPhoto 服务端 API

基础地址：`http://<host>:8000`；Web 管理台：`GET /`（上传、内容库、设备、OTA）。

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

## 数据存储

- SQLite：`services/framedphoto.db`（表：devices / images / firmware）
- 图片文件：`services/uploads/`；固件：`services/ota/`
- 均通过 `.env` 可配置（`DB_PATH` / `UPLOAD_DIR` / `OTA_DIR`）

## 固件对接（FPS6 格式）

见 [docs/protocol/fps6-format.md](fps6-format.md)。
