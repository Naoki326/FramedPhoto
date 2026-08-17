/*
 * http_util.c — HTTP 传输 deep module 核心（宿主机可移植，不依赖 ESP-IDF 头）
 *
 * 职责：组装请求 → 经可注入的传输后端逐块收集响应（grow-buffer，512 起步
 * 翻倍，NUL 结尾）→ 缓冲清理 → cJSON 解析 → 错误码映射。
 * esp_http_client 的事件回调注册与超时设置在 http_util_esp.c（固件后端）。
 */
#include <stdlib.h>
#include <string.h>
#include "esp_err.h"
#include "esp_log.h"
#include "cJSON.h"
#include "http_util.h"

#ifndef HTTP_UTIL_WEAK
#define HTTP_UTIL_WEAK __attribute__((weak))
#endif

static const char *TAG = "http_util";

/* ---------------- 传输后端：注入 + 弱默认 ---------------- */

static http_util_perform_t s_perform;
static void *s_perform_ctx;

void http_util_set_transport(http_util_perform_t perform, void *ctx)
{
    s_perform = perform;
    s_perform_ctx = ctx;
}

/* 弱默认：host 测试未注入时明确失败，而不是隐式成功 */
HTTP_UTIL_WEAK esp_err_t http_util_default_perform(void *ctx, const http_util_request_t *req,
                                                   http_util_chunk_cb_t on_chunk, void *on_chunk_ctx)
{
    (void)ctx;
    (void)req;
    (void)on_chunk;
    (void)on_chunk_ctx;
    return ESP_ERR_NOT_SUPPORTED;
}

/* ---------------- grow-buffer 收集器 ---------------- */

typedef struct {
    char *body;
    size_t len;
    size_t cap;
} collect_ctx_t;

static void collect_reset(collect_ctx_t *c)
{
    free(c->body);
    c->body = NULL;
    c->len = 0;
    c->cap = 0;
}

static esp_err_t collect_append(collect_ctx_t *c, const char *data, int len)
{
    if (len <= 0 || !data) {
        return ESP_ERR_INVALID_ARG;
    }
    if (c->len + (size_t)len + 1 > c->cap) {
        size_t ncap = c->cap ? c->cap * 2 : 512;
        while (c->len + (size_t)len + 1 > ncap) {
            ncap *= 2;
        }
        char *nb = realloc(c->body, ncap);
        if (!nb) {
            return ESP_ERR_NO_MEM;
        }
        c->body = nb;
        c->cap = ncap;
    }
    memcpy(c->body + c->len, data, (size_t)len);
    c->len += (size_t)len;
    c->body[c->len] = '\0';
    return ESP_OK;
}

static esp_err_t on_chunk(void *ctx, const char *data, int len)
{
    return collect_append((collect_ctx_t *)ctx, data, len);
}

/* ---------------- 单次请求 + JSON 解析 ---------------- */

static cJSON *request_json(const http_util_request_t *req, esp_err_t *err_out)
{
    if (err_out) {
        *err_out = ESP_OK;
    }
    if (!req->url || req->timeout_ms <= 0) {
        if (err_out) {
            *err_out = ESP_ERR_INVALID_ARG;
        }
        return NULL;
    }

    http_util_perform_t perform = s_perform ? s_perform : http_util_default_perform;
    void *perform_ctx = s_perform ? s_perform_ctx : NULL;

    collect_ctx_t ctx = { 0 };
    esp_err_t err = perform(perform_ctx, req, on_chunk, &ctx);
    if (err != ESP_OK) {
        /* 传输失败（超时/断流等）原样透传错误码；缓冲清理是 implementation 细节 */
        ESP_LOGW(TAG, "%s %s: %s", req->post_body ? "POST" : "GET", req->url,
                 esp_err_to_name(err));
        collect_reset(&ctx);
        if (err_out) {
            *err_out = err;
        }
        return NULL;
    }
    if (!ctx.body) {
        /* 零字节响应：无 JSON 可解析 */
        if (err_out) {
            *err_out = ESP_ERR_INVALID_RESPONSE;
        }
        return NULL;
    }

    cJSON *root = cJSON_Parse(ctx.body);
    collect_reset(&ctx);
    if (!root) {
        /* 半包 / 非法 JSON */
        if (err_out) {
            *err_out = ESP_ERR_INVALID_RESPONSE;
        }
        return NULL;
    }
    return root;
}

cJSON *http_util_get_json(const char *url, int timeout_ms, esp_err_t *err_out)
{
    http_util_request_t req = {
        .url = url,
        .post_body = NULL,
        .timeout_ms = timeout_ms,
    };
    return request_json(&req, err_out);
}

cJSON *http_util_post_json(const char *url, const char *json_body, int timeout_ms, esp_err_t *err_out)
{
    if (!json_body) {
        if (err_out) {
            *err_out = ESP_ERR_INVALID_ARG;
        }
        return NULL;
    }
    http_util_request_t req = {
        .url = url,
        .post_body = json_body,
        .timeout_ms = timeout_ms,
    };
    return request_json(&req, err_out);
}
