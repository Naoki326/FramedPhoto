/*
 * content_client.c — 服务端内容拉取（esp_http_client 事件模式）
 *
 * 基于 ESP-IDF esp_http_client 实现；图片下载走 on_data 回调写文件，
 * 不占大块 RAM。
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "esp_check.h"
#include "esp_log.h"
#include "esp_http_client.h"
#include "cJSON.h"
#include "content_client.h"
#include "fps6_format.h"

static const char *TAG = "content";

#define HTTP_TIMEOUT_MS 15000
#define MAX_MANIFEST_SIZE 4096

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

typedef struct {
    char *body;
    size_t len;
    size_t cap;
    esp_err_t err;
} collect_ctx_t;

static void collect_free(collect_ctx_t *c)
{
    if (c->body) {
        free(c->body);
        c->body = NULL;
    }
    c->len = c->cap = 0;
}

static esp_err_t collect_append(collect_ctx_t *c, const char *data, int len)
{
    if (c->len + (size_t)len + 1 > c->cap) {
        size_t ncap = c->cap ? c->cap * 2 : 512;
        while (c->len + (size_t)len + 1 > ncap) {
            ncap *= 2;
        }
        char *nb = realloc(c->body, ncap);
        if (!nb) {
            c->err = ESP_ERR_NO_MEM;
            return c->err;
        }
        c->body = nb;
        c->cap = ncap;
    }
    memcpy(c->body + c->len, data, len);
    c->len += len;
    c->body[c->len] = '\0';
    return ESP_OK;
}

static esp_err_t manifest_handler(esp_http_client_event_t *evt)
{
    collect_ctx_t *c = (collect_ctx_t *)evt->user_data;
    switch (evt->event_id) {
    case HTTP_EVENT_ON_DATA:
        return collect_append(c, evt->data, evt->data_len);
    case HTTP_EVENT_ON_FINISH:
        return ESP_OK;
    default:
        return ESP_OK;
    }
}

esp_err_t content_fetch_current(char *out_id, size_t id_len,
                                char *out_url, size_t url_len)
{
    char url[256];
    build_url(url, sizeof(url), "%s/api/images/content", NULL);

    collect_ctx_t ctx = { 0 };
    esp_http_client_config_t cfg = {
        .url = url,
        .timeout_ms = HTTP_TIMEOUT_MS,
        .event_handler = manifest_handler,
        .user_data = &ctx,
        .keep_alive_enable = false,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (!client) {
        return ESP_ERR_NO_MEM;
    }

    esp_err_t err = esp_http_client_perform(client);
    esp_http_client_cleanup(client);

    ESP_RETURN_ON_ERROR(err, TAG, "http %s", url);
    if (!ctx.body) {
        return ESP_ERR_INVALID_RESPONSE;
    }

    cJSON *root = cJSON_Parse(ctx.body);
    collect_free(&ctx);
    if (!root) {
        return ESP_ERR_INVALID_RESPONSE;
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

#include "esp_partition.h"

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
        .timeout_ms = HTTP_TIMEOUT_MS * 8, /* 960KB 下载放宽超时 */
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
