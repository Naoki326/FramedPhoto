/*
 * test_http_util.c — http_util 传输层宿主机测试
 *
 * 事件钩子（esp_http_client 的分块 ON_DATA 投递）以注入的 fake transport 提供：
 * 走 http_util 公共接口（get/post JSON），只断言对外可见的成败与输出。
 * 覆盖（票 #22 验收）：分块大响应完整收集、超时路径、半包与非法 JSON 失败路径、
 * 超时为调用方参数。
 *
 * 运行：make（在 firmware/host_tests/ 下）
 */
#include <stdlib.h>
#include <string.h>

#include "minitest.h"
#include "http_util.h"
#include "cJSON.h"

/* ---------------- fake transport：记录请求 + 按脚本分块交付 ---------------- */

typedef struct {
    /* 记录请求（transport 契约面） */
    const char *url_seen;
    const char *post_body_seen;
    int timeout_seen[8];
    int n_timeout;
    /* 分块交付脚本 */
    const char *body;            /* 完整响应体（chunk_size 切块交付）；NULL = 零分块 */
    int chunk_size;
    esp_err_t result;            /* 交付结束后的 perform 返回值（超时等） */
    int fail_after_chunks;       /* 交付 N 块后中断并返回 result；-1 = 交付完再返回 */
} fake_transport_t;

static void fake_reset(fake_transport_t *f, const char *body, int chunk_size,
                       esp_err_t result, int fail_after_chunks)
{
    memset(f, 0, sizeof(*f));
    f->body = body;
    f->chunk_size = chunk_size;
    f->result = result;
    f->fail_after_chunks = fail_after_chunks;
}

static esp_err_t fake_perform(void *ctx, const http_util_request_t *req,
                              http_util_chunk_cb_t on_chunk, void *on_chunk_ctx)
{
    fake_transport_t *f = (fake_transport_t *)ctx;
    f->url_seen = req->url;
    f->post_body_seen = req->post_body;
    if (f->n_timeout < (int)(sizeof(f->timeout_seen) / sizeof(f->timeout_seen[0]))) {
        f->timeout_seen[f->n_timeout++] = req->timeout_ms;
    }

    size_t total = f->body ? strlen(f->body) : 0;
    size_t off = 0;
    int delivered = 0;
    while (off < total) {
        if (f->fail_after_chunks >= 0 && delivered >= f->fail_after_chunks) {
            break; /* 半途断流（超时/连接中断） */
        }
        size_t take = (size_t)f->chunk_size;
        if (off + take > total) {
            take = total - off;
        }
        esp_err_t e = on_chunk(on_chunk_ctx, f->body + off, (int)take);
        if (e != ESP_OK) {
            return e; /* 传输层尊重收集中止 */
        }
        off += take;
        delivered++;
    }
    return f->result;
}

/* ---------------- 测试 ---------------- */

/* 构造已知良好的大 JSON：{"images":[{"id":"img-000"},...]} 共 n 项 */
static char *build_manifest(int n)
{
    /* 每项约 20 字节 + 外层结构；n=200 → 约 4.3KB，跨多次缓冲扩容 */
    size_t cap = (size_t)n * 24 + 32;
    char *s = malloc(cap);
    strcpy(s, "{\"images\":[");
    for (int i = 0; i < n; i++) {
        sprintf(s + strlen(s), "%s{\"id\":\"img-%03d\"}", i ? "," : "", i);
    }
    strcat(s, "]}");
    return s;
}

static void test_get_json_collects_chunked_large_response(void)
{
    enum { N = 200 };
    char *body = build_manifest(N);

    fake_transport_t fake;
    fake_reset(&fake, body, 7, ESP_OK, -1); /* 7 字节分块：4.3KB 拆 ~600 块，多次扩容 */
    http_util_set_transport(fake_perform, &fake);

    esp_err_t err = ESP_FAIL;
    cJSON *root = http_util_get_json("http://srv/api/images/content", 15000, &err);
    MT_ASSERT(root != NULL);
    MT_ASSERT_INT_EQ(err, ESP_OK);
    cJSON *images = cJSON_GetObjectItem(root, "images");
    MT_ASSERT(images != NULL);
    MT_ASSERT_INT_EQ(cJSON_GetArraySize(images), N);
    /* 期望值独立于分块方式：采样首/中/尾三项的字面量 */
    for (int i = 0; i < N; i += N / 2) {
        char want[16];
        sprintf(want, "img-%03d", i);
        cJSON *item = cJSON_GetArrayItem(images, i);
        MT_ASSERT(item != NULL);
        MT_ASSERT_STR_EQ(cJSON_GetObjectItem(item, "id")->valuestring, want);
    }
    cJSON_Delete(root);
    free(body);
}

/* 超时路径：传输层交付若干块后超时中断 → 错误码透传，不返回 JSON */
static void test_timeout_error_propagates(void)
{
    char *body = build_manifest(200);

    fake_transport_t fake;
    fake_reset(&fake, body, 64, ESP_ERR_TIMEOUT, 3); /* 交 3 块后超时断流 */
    http_util_set_transport(fake_perform, &fake);

    esp_err_t err = ESP_OK;
    cJSON *root = http_util_get_json("http://srv/api/images/content", 15000, &err);
    MT_ASSERT(root == NULL);
    MT_ASSERT_INT_EQ(err, ESP_ERR_TIMEOUT);
    free(body);
}

/* 半包：传输正常结束但响应体被截断（JSON 不完整）→ 解析失败 */
static void test_truncated_json_fails(void)
{
    const char *half = "{\"images\":[{\"id\":\"img-000\"},{\"id\":\"img-0";

    fake_transport_t fake;
    fake_reset(&fake, half, 16, ESP_OK, -1);
    http_util_set_transport(fake_perform, &fake);

    esp_err_t err = ESP_OK;
    cJSON *root = http_util_get_json("http://srv/api/ota/manifest", 60000, &err);
    MT_ASSERT(root == NULL);
    MT_ASSERT_INT_EQ(err, ESP_ERR_INVALID_RESPONSE);
}

/* 非法 JSON：传输成功但 body 不是 JSON（如网关 HTML 错误页） */
static void test_invalid_json_fails(void)
{
    fake_transport_t fake;
    fake_reset(&fake, "<html>502 Bad Gateway</html>", 8, ESP_OK, -1);
    http_util_set_transport(fake_perform, &fake);

    esp_err_t err = ESP_OK;
    cJSON *root = http_util_get_json("http://srv/api/images/content", 15000, &err);
    MT_ASSERT(root == NULL);
    MT_ASSERT_INT_EQ(err, ESP_ERR_INVALID_RESPONSE);
}

/* 空响应：零分块交付 → 无 JSON 可解析 */
static void test_empty_body_fails(void)
{
    fake_transport_t fake;
    fake_reset(&fake, NULL, 64, ESP_OK, -1);
    http_util_set_transport(fake_perform, &fake);

    esp_err_t err = ESP_OK;
    cJSON *root = http_util_get_json("http://srv/api/images/content", 15000, &err);
    MT_ASSERT(root == NULL);
    MT_ASSERT_INT_EQ(err, ESP_ERR_INVALID_RESPONSE);
}

/* 超时为调用方参数：不同值透传到 transport，不内置单一值 */
static void test_timeout_is_caller_supplied(void)
{
    fake_transport_t fake;
    fake_reset(&fake, "{\"ok\":true}", 8, ESP_OK, -1);
    http_util_set_transport(fake_perform, &fake);

    cJSON *a = http_util_get_json("http://srv/a", 15000, NULL);   /* 内容常规 */
    cJSON *b = http_util_get_json("http://srv/b", 120000, NULL);  /* OTA 最宽 */
    MT_ASSERT(a != NULL && b != NULL);
    cJSON_Delete(a);
    cJSON_Delete(b);

    MT_ASSERT_INT_EQ(fake.n_timeout, 2);
    MT_ASSERT_INT_EQ(fake.timeout_seen[0], 15000);
    MT_ASSERT_INT_EQ(fake.timeout_seen[1], 120000);
    /* GET 不携带 body（方法由 post_body 有无决定） */
    MT_ASSERT(fake.post_body_seen == NULL);
    MT_ASSERT_STR_EQ(fake.url_seen, "http://srv/b");
}

/* post-JSON：body 透传到 transport，响应正常解析 */
static void test_post_json_sends_body_and_parses_response(void)
{
    fake_transport_t fake;
    fake_reset(&fake, "{\"wifi_config\":null}", 8, ESP_OK, -1);
    http_util_set_transport(fake_perform, &fake);

    const char *body = "{\"firmware_version\":\"v1\",\"ip\":\"192.168.1.8\"}";
    esp_err_t err = ESP_OK;
    cJSON *root = http_util_post_json("http://srv/api/devices/esp32s3-abc/heartbeat",
                                      body, 15000, &err);
    MT_ASSERT(root != NULL);
    MT_ASSERT_INT_EQ(err, ESP_OK);
    MT_ASSERT(cJSON_GetObjectItem(root, "wifi_config") != NULL);
    cJSON_Delete(root);

    MT_ASSERT_STR_EQ(fake.url_seen, "http://srv/api/devices/esp32s3-abc/heartbeat");
    MT_ASSERT_STR_EQ(fake.post_body_seen, body);
}

/* 参数校验：坏参数不进入传输层 */
static void test_invalid_args_rejected(void)
{
    fake_transport_t fake;
    fake_reset(&fake, "{}", 8, ESP_OK, -1);
    http_util_set_transport(fake_perform, &fake);

    esp_err_t err = ESP_OK;
    MT_ASSERT(http_util_get_json(NULL, 15000, &err) == NULL);
    MT_ASSERT_INT_EQ(err, ESP_ERR_INVALID_ARG);

    err = ESP_OK;
    MT_ASSERT(http_util_get_json("http://srv/a", 0, &err) == NULL);
    MT_ASSERT_INT_EQ(err, ESP_ERR_INVALID_ARG);

    err = ESP_OK;
    MT_ASSERT(http_util_post_json("http://srv/a", NULL, 15000, &err) == NULL);
    MT_ASSERT_INT_EQ(err, ESP_ERR_INVALID_ARG);

    err = ESP_OK;
    MT_ASSERT(http_util_post_json("http://srv/a", "{}", -1, &err) == NULL);
    MT_ASSERT_INT_EQ(err, ESP_ERR_INVALID_ARG);

    MT_ASSERT_INT_EQ(fake.n_timeout, 0); /* 全部在入口拦截，未触达 transport */
}

/* 未注入且未覆盖后端：弱默认明确报 ESP_ERR_NOT_SUPPORTED */
static void test_no_transport_not_supported(void)
{
    http_util_set_transport(NULL, NULL);

    esp_err_t err = ESP_OK;
    MT_ASSERT(http_util_get_json("http://srv/a", 15000, &err) == NULL);
    MT_ASSERT_INT_EQ(err, ESP_ERR_NOT_SUPPORTED);
}

int main(void)
{
    MT_RUN(test_get_json_collects_chunked_large_response);
    MT_RUN(test_timeout_error_propagates);
    MT_RUN(test_truncated_json_fails);
    MT_RUN(test_invalid_json_fails);
    MT_RUN(test_empty_body_fails);
    MT_RUN(test_timeout_is_caller_supplied);
    MT_RUN(test_post_json_sends_body_and_parses_response);
    MT_RUN(test_invalid_args_rejected);
    MT_RUN(test_no_transport_not_supported);
    printf(mt_failures ? "FAILED (%d assertion%s)\n" : "OK (%d assertion%s)\n",
           mt_failures, mt_failures == 1 ? "" : "s");
    return MT_RESULT();
}
