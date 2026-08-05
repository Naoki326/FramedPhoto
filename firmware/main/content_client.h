/*
 * content_client.h — 服务端内容拉取
 *
 * 职责：
 *   1. GET {server}/api/images/content → 解析内容清单，取当前应显示图片的 id 与下载 url
 *   2. GET {url}                      → 下载 FPS6 数据到本地文件（storage 分区）
 *
 * 下载 url 直接取服务端 content 响应中的 url 字段（相对路径自动拼服务端前缀），
 * 不依赖「id 拼 URL」约定——服务端新增内容类型无需固件改动。
 */
#pragma once

#include "esp_err.h"
#include "esp_partition.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * 内容清单中当前应显示图片的 id 与下载地址。
 * @param out_id   图片 id（用于与上次显示内容比较，判断是否变化）
 * @param out_url  下载地址（已拼好服务端前缀的完整 URL）
 * @return ESP_OK；无内容返回 ESP_ERR_NOT_FOUND
 */
esp_err_t content_fetch_current(char *out_id, size_t id_len,
                                char *out_url, size_t url_len);

/**
 * 下载 FPS6 图片并裸写入 storage 分区（调用前须已擦除分区）。
 * @param url  完整下载地址（content_fetch_current 返回的 out_url）
 * @param part storage 分区句柄
 */
esp_err_t content_download_image(const char *url, const esp_partition_t *part);

#ifdef __cplusplus
}
#endif
