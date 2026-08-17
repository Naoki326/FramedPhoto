/*
 * http_util.h — 固件 HTTP 传输 deep module（get-JSON / post-JSON）
 *
 * grow-buffer 响应收集、事件回调注册、缓冲清理、超时设置全部是
 * implementation 细节；超时由调用方传参（内容常规 / 内容大图放宽 /
 * OTA 最宽的差异以参数保留），本模块不内置任何超时值。
 * 成功返回已解析的 cJSON 对象（调用方负责 cJSON_Delete），协议语义
 * （取哪个字段、无新内容怎么判）留在各 client。
 *
 * 传输后端可注入（host 测试以 fake transport 驱动，见 host_tests/）；
 * 固件默认后端为 esp_http_client 适配层（http_util_esp.c，链接期
 * 强符号覆盖 http_util_default_perform）。
 */
#ifndef HTTP_UTIL_H
#define HTTP_UTIL_H

#include <stddef.h>
#include "esp_err.h"
#include "cJSON.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 单次请求描述（由 get/post 入口组装，transport 后端消费） */
typedef struct {
    const char *url;       /* 完整 URL */
    const char *post_body; /* NULL → GET；非 NULL → POST（JSON body，Content-Type 由后端设置） */
    int timeout_ms;        /* 调用方传入的传输超时；无内置默认值 */
} http_util_request_t;

/* 传输层逐块回调：收到一段响应 body。返回非 ESP_OK 表示收集中止（如缓冲耗尽）。 */
typedef esp_err_t (*http_util_chunk_cb_t)(void *ctx, const char *data, int len);

/* 传输后端：执行一次 HTTP 请求，响应 body 分块经 on_chunk 交付；
 * 返回传输结果码（超时、连接失败等原样透传给调用方）。 */
typedef esp_err_t (*http_util_perform_t)(void *ctx, const http_util_request_t *req,
                                         http_util_chunk_cb_t on_chunk, void *on_chunk_ctx);

/* 注入传输后端（host 测试用）。注入优先于默认后端；传 NULL 恢复默认。 */
void http_util_set_transport(http_util_perform_t perform, void *ctx);

/* GET + 解析 JSON 响应。成功返回 cJSON 对象，失败返回 NULL 并（若非 NULL）回填 *err_out。 */
cJSON *http_util_get_json(const char *url, int timeout_ms, esp_err_t *err_out);

/* POST JSON body + 解析 JSON 响应。返回值约定同上。 */
cJSON *http_util_post_json(const char *url, const char *json_body, int timeout_ms, esp_err_t *err_out);

/* 默认传输后端。http_util.c 提供弱实现（未覆盖且未注入时返回 ESP_ERR_NOT_SUPPORTED）；
 * 固件构建由 http_util_esp.c 以 esp_http_client 实现强覆盖。 */
esp_err_t http_util_default_perform(void *ctx, const http_util_request_t *req,
                                    http_util_chunk_cb_t on_chunk, void *on_chunk_ctx);

#ifdef __cplusplus
}
#endif

#endif /* HTTP_UTIL_H */
