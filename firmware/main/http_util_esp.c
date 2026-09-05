/*
 * http_util_esp.c — http_util 的 esp_http_client 传输后端（固件默认后端）
 *
 * 事件回调注册（esp_http_client 事件 → 逐块回调）、超时设置、POST 头与
 * body 设置、连接清理全部收在本文件，对 http_util 调用方不可见。
 * 安装方式：app_main 调用 http_util_esp_install() 运行时注入（http_util_set_transport），
 * 不依赖链接期强符号覆盖弱默认（归档顺序会导致后端丢失，见 http_util_esp.h 教训）。
 */
#include <string.h>
#include "esp_err.h"
#include "esp_http_client.h"
#include "http_util.h"
#include "http_util_esp.h"

typedef struct {
    http_util_chunk_cb_t cb;
    void *cb_ctx;
} chunk_bridge_t;

static esp_err_t http_event_handler(esp_http_client_event_t *evt)
{
    chunk_bridge_t *b = (chunk_bridge_t *)evt->user_data;
    if (evt->event_id == HTTP_EVENT_ON_DATA && evt->data_len > 0) {
        return b->cb(b->cb_ctx, evt->data, evt->data_len);
    }
    return ESP_OK;
}

static esp_err_t http_util_esp_perform(void *ctx, const http_util_request_t *req,
                                       http_util_chunk_cb_t on_chunk, void *on_chunk_ctx)
{
    (void)ctx;
    chunk_bridge_t bridge = { .cb = on_chunk, .cb_ctx = on_chunk_ctx };

    esp_http_client_config_t cfg = {
        .url = req->url,
        .timeout_ms = req->timeout_ms,
        .event_handler = http_event_handler,
        .user_data = &bridge,
        .keep_alive_enable = false,
    };
    if (req->post_body) {
        cfg.method = HTTP_METHOD_POST;
    }

    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (!client) {
        return ESP_ERR_NO_MEM;
    }
    if (req->post_body) {
        esp_http_client_set_header(client, "Content-Type", "application/json");
        esp_http_client_set_post_field(client, req->post_body, strlen(req->post_body));
    }

    esp_err_t err = esp_http_client_perform(client);
    esp_http_client_cleanup(client);
    return err;
}

void http_util_esp_install(void)
{
    http_util_set_transport(http_util_esp_perform, NULL);
}
