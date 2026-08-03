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

esp_err_t content_fetch_current_id(char *out_id, size_t len)
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

    esp_err_t ret = ESP_ERR_NOT_FOUND;
    if (cJSON_IsString(id)) {
        snprintf(out_id, len, "%s", id->valuestring);
        ret = ESP_OK;
        ESP_LOGI(TAG, "current image: %s", out_id);
    } else {
        ESP_LOGW(TAG, "content list empty");
    }
    cJSON_Delete(root);
    return ret;
}

/* ---------------- 图片下载 ---------------- */

typedef struct {
    FILE *fp;
    esp_err_t err;
} dl_ctx_t;

static esp_err_t image_handler(esp_http_client_event_t *evt)
{
    dl_ctx_t *d = (dl_ctx_t *)evt->user_data;
    switch (evt->event_id) {
    case HTTP_EVENT_ON_DATA:
        if (d->fp && evt->data_len > 0) {
            if (fwrite(evt->data, 1, evt->data_len, d->fp) != (size_t)evt->data_len) {
                d->err = ESP_ERR_NO_MEM; /* 磁盘写失败（磁盘满） */
            }
        }
        return ESP_OK;
    default:
        return ESP_OK;
    }
}

esp_err_t content_download_image(const char *id, const char *path)
{
    char url[256];
    build_url(url, sizeof(url), "%s/api/images/%s/raw", id);

    dl_ctx_t ctx = { .fp = fopen(path, "wb"), .err = ESP_OK };
    if (!ctx.fp) {
        ESP_LOGE(TAG, "open %s failed", path);
        return ESP_FAIL;
    }

    esp_http_client_config_t cfg = {
        .url = url,
        .timeout_ms = HTTP_TIMEOUT_MS * 8, /* 960KB 下载放宽超时 */
        .event_handler = image_handler,
        .user_data = &ctx,
        .keep_alive_enable = false,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    esp_err_t err = ESP_ERR_NO_MEM;
    if (client) {
        err = esp_http_client_perform(client);
        esp_http_client_cleanup(client);
    }
    fclose(ctx.fp);

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "download %s failed: %s", url, esp_err_to_name(err));
        return err;
    }
    if (ctx.err != ESP_OK) {
        ESP_LOGE(TAG, "write %s failed (disk full?)", path);
        return ctx.err;
    }

    /* 校验文件大小（FPS6 数据区 = 960000B + 20B 头） */
    FILE *fp = fopen(path, "rb");
    long size = 0;
    if (fp) {
        fseek(fp, 0, SEEK_END);
        size = ftell(fp);
        fclose(fp);
    }
    if (size < 960000 + 20) {
        ESP_LOGE(TAG, "file too small: %ld", size);
        return ESP_ERR_INVALID_SIZE;
    }
    ESP_LOGI(TAG, "downloaded %s (%ld bytes)", id, size);
    return ESP_OK;
}
