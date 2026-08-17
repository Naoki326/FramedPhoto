/*
 * content_client.c — 服务端内容拉取
 *
 * 内容清单（JSON）走 http_util：grow-buffer 收集、事件回调注册、清理、
 * 超时全部是传输层 implementation 细节，本 client 只保留协议职责
 * （清单解析 + 下载地址拼装）。图片下载是二进制流式写 storage 裸分区
 * （帧头校验 + 分区写入为本 client 的协议职责，快于文件系统），
 * 仍走 esp_http_client 事件模式，不经 JSON 缓冲。
 */
#include <stdio.h>
#include <string.h>
#include "esp_log.h"
#include "esp_http_client.h"
#include "esp_partition.h"
#include "cJSON.h"
#include "http_util.h"
#include "content_client.h"
#include "fps6_format.h"

static const char *TAG = "content";

static esp_err_t build_url(char *buf, size_t len, const char *fmt, const char *arg)
{
    if (arg) {
        snprintf(buf, len, fmt, CONFIG_FRAMEDPHOTO_SERVER_URL, arg);
    } else {
        snprintf(buf, len, fmt, CONFIG_FRAMEDPHOTO_SERVER_URL);
    }
    return ESP_OK;
}

/* ---------------- 内容清单 ---------------- */

esp_err_t content_fetch_current(char *out_id, size_t id_len,
                                char *out_url, size_t url_len)
{
    char url[256];
    build_url(url, sizeof(url), "%s/api/images/content", NULL);

    /* 内容清单常规超时 15s（大图下载另放宽，见 content_download_image） */
    esp_err_t err = ESP_OK;
    cJSON *root = http_util_get_json(url, 15000, &err);
    if (!root) {
        ESP_LOGW(TAG, "content manifest %s: %s", url, esp_err_to_name(err));
        return err;
    }

    cJSON *images = cJSON_GetObjectItem(root, "images");
    cJSON *first = images && cJSON_GetArraySize(images) > 0
                       ? cJSON_GetArrayItem(images, 0) : NULL;
    cJSON *id = first ? cJSON_GetObjectItem(first, "id") : NULL;
    cJSON *dl  = first ? cJSON_GetObjectItem(first, "url") : NULL;

    esp_err_t ret = ESP_ERR_NOT_FOUND;
    if (cJSON_IsString(id)) {
        snprintf(out_id, id_len, "%s", id->valuestring);
        /* 下载地址：优先用服务端返回的 url 字段（相对路径拼前缀），
         * 避免「id 拼 URL」约定导致服务端新增内容类型时 404。 */
        if (cJSON_IsString(dl) && dl->valuestring[0] != '\0') {
            const char *u = dl->valuestring;
            if (u[0] == '/') {
                snprintf(out_url, url_len, "%s%s", CONFIG_FRAMEDPHOTO_SERVER_URL, u);
            } else {
                snprintf(out_url, url_len, "%s", u);
            }
        } else {
            build_url(out_url, url_len, "%s/api/images/%s/raw", id->valuestring);
        }
        ret = ESP_OK;
        ESP_LOGI(TAG, "current image: %s (url %s)", out_id, out_url);
    } else {
        ESP_LOGW(TAG, "content list empty");
    }
    cJSON_Delete(root);
    return ret;
}

/* ---------------- 图片下载（写 storage 裸分区，快于文件系统） ---------------- */

typedef struct {
    const esp_partition_t *part;
    size_t written;   /* 已写入偏移 */
    size_t got;       /* 帧头缓冲已收字节数；校验通过前不写分区 */
    uint8_t header[FPS6_HEADER_SIZE];
    esp_err_t err;
} dl_ctx_t;

static uint32_t le32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 |
           (uint32_t)p[3] << 24;
}

/* 帧头校验：magic 与宽高（格式见 docs/protocol/fps6-format.md，常量见 fps6_format.h）。
 * 不符 → 按下载失败返回：坏帧零字节落分区、不刷屏，等下次心跳重试。 */
static esp_err_t frame_header_check(const uint8_t *h)
{
    if (memcmp(h, FPS6_MAGIC, 4) != 0) {
        ESP_LOGE(TAG, "bad frame magic: %02x %02x %02x %02x", h[0], h[1], h[2], h[3]);
        return ESP_ERR_INVALID_RESPONSE;
    }
    uint32_t width = le32(h + 4);
    uint32_t height = le32(h + 8);
    if (width != FPS6_FRAME_WIDTH || height != FPS6_FRAME_HEIGHT) {
        ESP_LOGE(TAG, "frame size mismatch: %ux%u (expect %dx%d)",
                 (unsigned)width, (unsigned)height,
                 FPS6_FRAME_WIDTH, FPS6_FRAME_HEIGHT);
        return ESP_ERR_INVALID_SIZE;
    }
    return ESP_OK;
}

static esp_err_t write_chunk(dl_ctx_t *d, const void *data, size_t len)
{
    esp_err_t e = esp_partition_write(d->part, d->written, data, len);
    if (e != ESP_OK) {
        ESP_LOGE(TAG, "partition write @%u: %s", (unsigned)d->written, esp_err_to_name(e));
        d->err = e;
        return ESP_FAIL;
    }
    d->written += len;
    return ESP_OK;
}

static esp_err_t image_handler(esp_http_client_event_t *evt)
{
    dl_ctx_t *d = (dl_ctx_t *)evt->user_data;
    if (evt->event_id != HTTP_EVENT_ON_DATA || evt->data_len <= 0 || d->err != ESP_OK) {
        return ESP_OK;
    }
    const char *p = evt->data;
    int len = evt->data_len;

    /* 帧头先缓冲、校验通过才写分区：恰好够长但头损坏的帧在此拒绝 */
    if (d->got < FPS6_HEADER_SIZE) {
        size_t take = FPS6_HEADER_SIZE - d->got;
        if (take > (size_t)len) {
            take = (size_t)len;
        }
        memcpy(d->header + d->got, p, take);
        d->got += take;
        p += take;
        len -= (int)take;
        if (d->got < FPS6_HEADER_SIZE) {
            return ESP_OK; /* 帧头未收满 */
        }
        d->err = frame_header_check(d->header);
        if (d->err != ESP_OK) {
            return ESP_FAIL; /* 坏帧：中止传输，不写分区 */
        }
        if (write_chunk(d, d->header, FPS6_HEADER_SIZE) != ESP_OK) {
            return ESP_FAIL;
        }
    }
    if (len > 0 && write_chunk(d, p, len) != ESP_OK) {
        return ESP_FAIL;
    }
    return ESP_OK;
}

/**
 * 下载 FPS6 图片并裸写入 storage 分区（调用前须已擦除分区）。
 * 帧头（magic/宽高）校验通过才写分区：坏帧按下载失败返回，
 * 不写分区、不触发刷屏，等下次心跳重试（常量见 fps6_format.h）。
 * @param url  完整下载地址（content_fetch_current 返回的 out_url）
 * @param part storage 分区句柄
 */
esp_err_t content_download_image(const char *url, const esp_partition_t *part)
{
    dl_ctx_t ctx = { .part = part, .written = 0, .err = ESP_OK };

    esp_http_client_config_t cfg = {
        .url = url,
        .timeout_ms = 120000, /* 960KB 大图下载放宽超时（清单常规 15s 的 8 倍） */
        .event_handler = image_handler,
        .user_data = &ctx,
        .keep_alive_enable = false,
        .buffer_size = 4096, /* 大缓冲：1MB 图少 4 倍 HTTP 回调，显著提速 */
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    esp_err_t err = ESP_ERR_NO_MEM;
    if (client) {
        err = esp_http_client_perform(client);
        esp_http_client_cleanup(client);
    }

    /* 帧头校验/分区写入失败的具体原因已在回调中记录（bad frame magic /
     * frame size mismatch / partition write），优先返回精确错误码 */
    if (ctx.err != ESP_OK) {
        return ctx.err;
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "download %s failed: %s", url, esp_err_to_name(err));
        return err;
    }

    /* 校验文件大小（整帧 = 头 20B + 数据区 960000B，常量见 fps6_format.h） */
    if (ctx.written < FPS6_FRAME_SIZE) {
        ESP_LOGE(TAG, "file too small: %u", (unsigned)ctx.written);
        return ESP_ERR_INVALID_SIZE;
    }
    ESP_LOGI(TAG, "downloaded %s (%u bytes)", url, (unsigned)ctx.written);
    return ESP_OK;
}
