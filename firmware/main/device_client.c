/*
 * device_client.c — 设备心跳上报 + 服务端 WiFi 配置拉取
 *
 * 流程：POST {server}/api/devices/{id}/heartbeat
 *   1. body 上报固件版本 / 当前内容 / IP / 运行时长；设备 id 由 MAC 派生，幂等
 *   2. 响应若带 wifi_config（服务端管理台设置的待下发配置）：
 *      写入 NVS -> 重启 -> 连新 WiFi -> 下次心跳带 wifi_config_applied=true
 *      服务端收到确认后清 pending，设备本地清 applied 标志
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "esp_check.h"
#include "esp_log.h"
#include "esp_http_client.h"
#include "esp_netif.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "cJSON.h"
#include "device_client.h"
#include "wifi_app.h"

static const char *TAG = "device";

#define HTTP_TIMEOUT_MS 15000
#define MAX_RESP_SIZE   2048

/* ---------------- 设备 id（MAC 派生，幂等） ---------------- */

static void device_id(char *out, size_t len)
{
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(out, len, "esp32s3-%02x%02x%02x", mac[3], mac[4], mac[5]);
}

/* ---------------- NVS 辅助 ---------------- */

static esp_err_t nvs_get_flag(const char *ns, const char *key, bool *out)
{
    nvs_handle_t h;
    *out = false;
    if (nvs_open(ns, NVS_READONLY, &h) == ESP_OK) {
        uint8_t v = 0;
        if (nvs_get_u8(h, key, &v) == ESP_OK) {
            *out = (v != 0);
        }
        nvs_close(h);
    }
    return ESP_OK;
}

static esp_err_t nvs_set_flag(const char *ns, const char *key, bool val)
{
    nvs_handle_t h;
    ESP_RETURN_ON_ERROR(nvs_open(ns, NVS_READWRITE, &h), TAG, "nvs open");
    esp_err_t err = nvs_set_u8(h, key, val ? 1 : 0);
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);
    return err;
}

static esp_err_t nvs_get_str_value(const char *ns, const char *key, char *out, size_t len)
{
    out[0] = '\0';
    nvs_handle_t h;
    if (nvs_open(ns, NVS_READONLY, &h) == ESP_OK) {
        size_t l = len;
        nvs_get_str(h, key, out, &l);
        nvs_close(h);
    }
    return ESP_OK;
}

static esp_err_t sta_ip(char *out, size_t len)
{
    esp_netif_t *nif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    esp_netif_ip_info_t ip;
    if (nif && esp_netif_get_ip_info(nif, &ip) == ESP_OK && ip.ip.addr != 0) {
        snprintf(out, len, IPSTR, IP2STR(&ip.ip));
        return ESP_OK;
    }
    out[0] = '\0';
    return ESP_ERR_NOT_FOUND;
}

/* ---------------- HTTP 响应收集 ---------------- */

typedef struct {
    char *body;
    size_t len;
    size_t cap;
} resp_ctx_t;

static void resp_free(resp_ctx_t *c)
{
    if (c->body) {
        free(c->body);
        c->body = NULL;
    }
    c->len = c->cap = 0;
}

static esp_err_t resp_append(resp_ctx_t *c, const char *data, int len)
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

static esp_err_t resp_handler(esp_http_client_event_t *evt)
{
    resp_ctx_t *c = (resp_ctx_t *)evt->user_data;
    if (evt->event_id == HTTP_EVENT_ON_DATA) {
        return resp_append(c, evt->data, evt->data_len);
    }
    return ESP_OK;
}

/* ---------------- 心跳 + WiFi 配置处理 ---------------- */

esp_err_t device_client_heartbeat(void)
{
    char id[32];
    device_id(id, sizeof(id));

    /* 当前内容（display 模块写入的 last_id） */
    char last_id[64];
    nvs_get_str_value("display", "last_id", last_id, sizeof(last_id));

    char ip[16];
    sta_ip(ip, sizeof(ip));

    bool applied_ack = false;
    nvs_get_flag("wifi", "applied_ack", &applied_ack);

    /* 组装 body */
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "firmware_version", CONFIG_FRAMEDPHOTO_FW_VERSION);
    cJSON_AddStringToObject(root, "current_image", last_id);
    cJSON_AddStringToObject(root, "ip", ip);
    cJSON_AddNumberToObject(root, "uptime_s",
                            (double)(esp_timer_get_time() / 1000000ULL));
    cJSON_AddBoolToObject(root, "wifi_config_applied", applied_ack);
    char *body = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!body) {
        return ESP_ERR_NO_MEM;
    }

    char url[256];
    snprintf(url, sizeof(url), "%s/api/devices/%s/heartbeat",
             CONFIG_FRAMEDPHOTO_SERVER_URL, id);

    resp_ctx_t ctx = { 0 };
    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = HTTP_TIMEOUT_MS,
        .event_handler = resp_handler,
        .user_data = &ctx,
        .keep_alive_enable = false,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    esp_err_t err = ESP_ERR_NO_MEM;
    if (client) {
        esp_http_client_set_header(client, "Content-Type", "application/json");
        esp_http_client_set_post_field(client, body, strlen(body));
        err = esp_http_client_perform(client);
        esp_http_client_cleanup(client);
    }
    free(body);

    if (err != ESP_OK) {
        ESP_LOGW(TAG, "heartbeat failed: %s", esp_err_to_name(err));
        resp_free(&ctx);
        return err;
    }

    /* 解析响应 */
    cJSON *resp = ctx.body ? cJSON_Parse(ctx.body) : NULL;
    resp_free(&ctx);
    if (!resp) {
        return ESP_ERR_INVALID_RESPONSE;
    }

    cJSON *wifi = cJSON_GetObjectItem(resp, "wifi_config");
    if (cJSON_IsObject(wifi)) {
        cJSON *ssid_j = cJSON_GetObjectItem(wifi, "ssid");
        cJSON *pass_j = cJSON_GetObjectItem(wifi, "password");
        const char *ssid = cJSON_IsString(ssid_j) ? ssid_j->valuestring : "";
        if (ssid[0] != '\0') {
            const char *pass = cJSON_IsString(pass_j) ? pass_j->valuestring : "";
            ESP_LOGI(TAG, "server pushed wifi config: '%s'", ssid);
            err = wifi_app_save_credentials(ssid, pass);
            if (err == ESP_OK) {
                nvs_set_flag("wifi", "applied_ack", true);
                cJSON_Delete(resp);
                ESP_LOGW(TAG, "wifi config applied, restarting...");
                vTaskDelay(pdMS_TO_TICKS(500));
                esp_restart();
            }
        }
    } else if (applied_ack) {
        /* 服务端已确认（pending 清除），本地清标志 */
        nvs_set_flag("wifi", "applied_ack", false);
        ESP_LOGI(TAG, "wifi applied acked by server");
    }
    cJSON_Delete(resp);
    ESP_LOGI(TAG, "heartbeat ok (id=%s ip=%s)", id, ip);
    return ESP_OK;
}
