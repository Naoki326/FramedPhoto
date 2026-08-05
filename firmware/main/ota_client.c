/*
 * ota_client.c — 设备端 OTA 客户端（esp_ota 分区切换）
 *
 * 流程（每次内容轮询 / 按钮唤醒时随 update_from_server 调用）：
 *   1. GET /api/ota/manifest?device_id=<id>&current_version=<运行版本>
 *      服务端判定无新固件 → 返回 ESP_ERR_NOT_FOUND，不打扰正常显示
 *   2. 有新固件 → 打开非活动 OTA 分区，流式下载（esp_http_client 事件
 *      模式逐块 esp_ota_write），同时 mbedtls_sha256 逐块计算
 *   3. 校验 sha256 + esp_ota_end（app 镜像头校验）→ 设置启动分区 → 重启
 *   4. 新固件启动后 ota_client_init() 标记有效（rollback 确认）；
 *      若新固件崩溃重启，bootloader 自动回滚旧分区
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "esp_check.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_http_client.h"
#include "esp_mac.h"
#include "esp_partition.h"
#include "psa/crypto.h"
#include "cJSON.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "ota_client.h"

static const char *TAG = "ota";

#define HTTP_TIMEOUT_MS    60000  /* 1MB+ 固件下载放宽超时 */
#define SHA256_HEX_LEN     64

/* ---------------- 设备 id（与 device_client 一致：MAC 派生） ---------------- */

static void device_id(char *out, size_t len)
{
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(out, len, "esp32s3-%02x%02x%02x", mac[3], mac[4], mac[5]);
}

/* ---------------- 响应收集 ---------------- */

typedef struct {
    char *body;
    size_t len;
    size_t cap;
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
            return ESP_ERR_NO_MEM;
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
    if (evt->event_id == HTTP_EVENT_ON_DATA && evt->data_len > 0) {
        return collect_append(c, evt->data, evt->data_len);
    }
    return ESP_OK;
}

/* ---------------- manifest 检查 ---------------- */

/* 返回 ESP_OK = 有新固件（回填版本/sha256/size）；ESP_ERR_NOT_FOUND = 无；其他 = 网络失败 */
static esp_err_t fetch_manifest(const char *current_ver,
                                char *new_ver, size_t ver_len,
                                char *sha256, size_t sha_len,
                                int *size_out)
{
    char id[32];
    device_id(id, sizeof(id));

    char url[384];
    snprintf(url, sizeof(url),
             "%s/api/ota/manifest?device_id=%s&current_version=%s",
             CONFIG_FRAMEDPHOTO_SERVER_URL, id, current_ver);

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
    if (err != ESP_OK || !ctx.body) {
        ESP_LOGE(TAG, "manifest request failed: %s (body=%s)",
                 esp_err_to_name(err), ctx.body ? ctx.body : "<empty>");
        collect_free(&ctx);
        return err != ESP_OK ? err : ESP_ERR_INVALID_RESPONSE;
    }

    ESP_LOGW(TAG, "manifest raw body [%u]: %.300s", (unsigned)ctx.len,
             ctx.body ? ctx.body : "<empty>");
    cJSON *root = cJSON_Parse(ctx.body);
    collect_free(&ctx);
    if (!root) {
        ESP_LOGE(TAG, "manifest json parse failed");
        return ESP_ERR_INVALID_RESPONSE;
    }

    esp_err_t ret = ESP_ERR_NOT_FOUND;
    cJSON *detail = cJSON_GetObjectItem(root, "detail");
    if (cJSON_IsObject(detail)) {
        cJSON *ver = cJSON_GetObjectItem(detail, "version");
        cJSON *sha = cJSON_GetObjectItem(detail, "sha256");
        cJSON *sz  = cJSON_GetObjectItem(detail, "size");
        if (cJSON_IsString(ver) && cJSON_IsString(sha)) {
            snprintf(new_ver, ver_len, "%s", ver->valuestring);
            snprintf(sha256, sha_len, "%s", sha->valuestring);
            *size_out = cJSON_IsNumber(sz) ? sz->valueint : 0;
            ret = ESP_OK;
            ESP_LOGI(TAG, "new firmware: %s (%d bytes)", new_ver, *size_out);
        }
    } else {
        ESP_LOGI(TAG, "no new firmware");
    }
    cJSON_Delete(root);
    return ret;
}

/* ---------------- 下载 + 写分区 + 校验 ---------------- */

typedef struct {
    esp_ota_handle_t handle;
    psa_hash_operation_t hash;
    size_t written;
    esp_err_t err;
} ota_dl_ctx_t;

static esp_err_t ota_download_handler(esp_http_client_event_t *evt)
{
    ota_dl_ctx_t *o = (ota_dl_ctx_t *)evt->user_data;
    if (evt->event_id == HTTP_EVENT_ON_DATA && evt->data_len > 0) {
        if (esp_ota_write(o->handle, evt->data, evt->data_len) != ESP_OK ||
            psa_hash_update(&o->hash, (const uint8_t *)evt->data, evt->data_len)
                != PSA_SUCCESS) {
            o->err = ESP_FAIL;
            return ESP_FAIL;
        }
        o->written += evt->data_len;
    }
    return ESP_OK;
}

static esp_err_t download_ota(const esp_partition_t *part, size_t expected_size,
                              const char *expected_sha)
{
    char url[256];
    snprintf(url, sizeof(url), "%s/api/ota/download", CONFIG_FRAMEDPHOTO_SERVER_URL);

    ota_dl_ctx_t ctx;
    memset(&ctx, 0, sizeof(ctx));
    ctx.hash = psa_hash_operation_init();
    if (psa_hash_setup(&ctx.hash, PSA_ALG_SHA_256) != PSA_SUCCESS) {
        ESP_LOGE(TAG, "psa_hash_setup failed");
        return ESP_FAIL;
    }

    /* expected_size 已知则传入以预校验容量，未知用 OTA_SIZE_UNKNOWN */
    esp_err_t err = esp_ota_begin(part, expected_size ? expected_size : OTA_SIZE_UNKNOWN,
                                  &ctx.handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_begin failed: %s", esp_err_to_name(err));
        return err;
    }

    esp_http_client_config_t cfg = {
        .url = url,
        .timeout_ms = HTTP_TIMEOUT_MS,
        .event_handler = ota_download_handler,
        .user_data = &ctx,
        .keep_alive_enable = false,
        .buffer_size = 4096,  /* 大缓冲减少 esp_ota_write 次数 */
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (!client) {
        esp_ota_abort(ctx.handle);
        return ESP_ERR_NO_MEM;
    }
    err = esp_http_client_perform(client);
    esp_http_client_cleanup(client);

    if (err != ESP_OK || ctx.err != ESP_OK) {
        ESP_LOGE(TAG, "download failed: %s", esp_err_to_name(err));
        psa_hash_abort(&ctx.hash);
        esp_ota_abort(ctx.handle);
        return err != ESP_OK ? err : ctx.err;
    }

    /* sha256 校验 */
    uint8_t digest[32];
    size_t digest_len = 0;
    char hex[SHA256_HEX_LEN + 1];
    if (psa_hash_finish(&ctx.hash, digest, sizeof(digest), &digest_len) != PSA_SUCCESS
        || digest_len != sizeof(digest)) {
        ESP_LOGE(TAG, "psa_hash_finish failed");
        psa_hash_abort(&ctx.hash);
        esp_ota_abort(ctx.handle);
        return ESP_FAIL;
    }
    for (int i = 0; i < 32; i++) {
        snprintf(&hex[i * 2], 3, "%02x", digest[i]);
    }
    if (expected_sha[0] && strcmp(hex, expected_sha) != 0) {
        ESP_LOGE(TAG, "sha256 mismatch: got %s expected %s", hex, expected_sha);
        esp_ota_abort(ctx.handle);
        return ESP_ERR_INVALID_CRC;
    }
    ESP_LOGI(TAG, "sha256 ok (%s), %u bytes written",
             hex, (unsigned)ctx.written);

    err = esp_ota_end(ctx.handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_end failed: %s", esp_err_to_name(err));
        return err;
    }
    return ESP_OK;
}

/* ---------------- 对外接口 ---------------- */

void ota_client_init(void)
{
    /* 配合 CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE：新固件首次正常启动
     * → 标记有效；若启动即崩溃，bootloader 自动回滚旧分区。 */
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t state;
    if (running && esp_ota_get_state_partition(running, &state) == ESP_OK &&
        state == ESP_OTA_IMG_PENDING_VERIFY) {
        ESP_LOGW(TAG, "new firmware booted, marking app valid");
        esp_ota_mark_app_valid_cancel_rollback();
    }
}

esp_err_t ota_client_maybe_update(void)
{
    /* 版本与心跳上报、服务端固件版本管理保持一致（Kconfig 宏）：
     * 发版时同步更新 FRAMEDPHOTO_FW_VERSION，上传固件填相同版本号。 */
    char cur[64];
    snprintf(cur, sizeof(cur), "%s", CONFIG_FRAMEDPHOTO_FW_VERSION);
    ESP_LOGI(TAG, "running version %s", cur);

    char new_ver[64], sha_hex[SHA256_HEX_LEN + 1];
    int size = 0;
    esp_err_t err = fetch_manifest(cur, new_ver, sizeof(new_ver),
                                   sha_hex, sizeof(sha_hex), &size);
    if (err != ESP_OK) {
        return err;  /* 无新固件或检查失败，均不打扰显示流程 */
    }
    if (strcmp(new_ver, cur) == 0) {
        ESP_LOGI(TAG, "manifest version equals running (%s), skip", cur);
        return ESP_ERR_NOT_FOUND;
    }

    const esp_partition_t *part = esp_ota_get_next_update_partition(NULL);
    if (!part) {
        ESP_LOGE(TAG, "no ota update partition");
        return ESP_ERR_NOT_FOUND;
    }
    ESP_LOGI(TAG, "updating on %s @0x%x (partition size %u)",
             part->label, part->address, part->size);

    err = download_ota(part, (size_t)size, sha_hex);
    if (err != ESP_OK) {
        return err;  /* 失败保持旧固件，下次轮询重试 */
    }

    err = esp_ota_set_boot_partition(part);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "set boot partition failed: %s", esp_err_to_name(err));
        return err;
    }
    ESP_LOGW(TAG, "OTA to %s done, rebooting...", new_ver);
    vTaskDelay(pdMS_TO_TICKS(500));
    esp_restart();
    return ESP_OK; /* unreachable */
}
