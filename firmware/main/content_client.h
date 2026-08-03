/*
 * content_client.h — 服务端内容拉取
 *
 * 职责：
 *   1. GET {server}/api/images/content  → 解析内容清单，取当前应显示图片
 *   2. GET {server}/api/images/{id}/raw → 下载 FPS6 数据到本地文件（storage 分区）
 *
 * TODO(M3): 设备注册/心跳/OTA 接口接入同源。
 */
#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 内容清单中当前应显示的图片 id（无内容返回 ESP_ERR_NOT_FOUND） */
esp_err_t content_fetch_current_id(char *out_id, size_t len);

/**
 * 下载 FPS6 图片到本地文件。
 * @param id     图片 id
 * @param path   目标文件路径（storage 分区，如 "/spiffs/last.fps6"）
 */
esp_err_t content_download_image(const char *id, const char *path);

#ifdef __cplusplus
}
#endif
