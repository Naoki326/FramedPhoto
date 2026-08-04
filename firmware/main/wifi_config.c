/*
 * wifi_config.c — 运行时配网（SmartConfig 主 + SoftAP 兑底）
 *
 * 设备无有效 WiFi 凭据时：
 *   1. 先进入 SmartConfig（ESPTouch v1）监听，服务端/App 在局域网广播配置，
 *      设备收到后写入 NVS 并重启连接——全程无人断网；
 *   2. 监听 SMARTCONFIG_TIMEOUT_S 超时后，开启 SoftAP 热点（FramedPhoto-XXXX），
 *      手机连上后浏览器访问 192.168.4.1 配网页兑底。
 */
#include <stdio.h>
#include <string.h>
#include "esp_check.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_http_server.h"
#include "esp_mac.h"
#include "esp_smartconfig.h"
#include "cJSON.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "wifi_config.h"
#include "wifi_app.h"

static const char *TAG = "wifi_config";

#define AP_SSID_MAX   32
#define SCAN_MAX_APS  20
#define SMARTCONFIG_TIMEOUT_S 300   /* 5 分钟后开 AP 兑底 */
#define SC_GOT_BIT     BIT0

/* ESPTouch v2 共享密钥（16 字节），须与服务端 esptouch.send_smartconfig_v2 一致 */
#define ESPTOUCH_V2_KEY "FramedPhoto2024!"

static EventGroupHandle_t s_sc_events;
static char s_ap_ssid[AP_SSID_MAX];

/* ---------------- SmartConfig 监听 ---------------- */

static void sc_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    if (base == SC_EVENT && id == SC_EVENT_GOT_SSID_PSWD) {
        smartconfig_event_got_ssid_pswd_t *evt =
            (smartconfig_event_got_ssid_pswd_t *)data;
        esp_err_t err = wifi_app_save_credentials(
            (const char *)evt->ssid, (const char *)evt->password);
        if (err == ESP_OK) {
            ESP_LOGI(TAG, "smartconfig got '%s'", (const char *)evt->ssid);
            xEventGroupSetBits(s_sc_events, SC_GOT_BIT);
        } else {
            ESP_LOGE(TAG, "save credentials failed");
        }
    }
}

static void smartconfig_task(void *arg)
{
    (void)arg;
    s_sc_events = xEventGroupCreate();

    esp_err_t err = esp_event_handler_register(
        SC_EVENT, ESP_EVENT_ANY_ID, &sc_event_handler, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "sc handler register: %s", esp_err_to_name(err));
        vTaskDelete(NULL);
        return;
    }
    esp_smartconfig_set_type(SC_TYPE_ESPTOUCH_V2);

    smartconfig_start_config_t cfg = SMARTCONFIG_START_CONFIG_DEFAULT();
    cfg.esp_touch_v2_enable_crypt = true;
    cfg.esp_touch_v2_key = (char *)ESPTOUCH_V2_KEY;
    err = esp_smartconfig_start(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "sc start: %s", esp_err_to_name(err));
        esp_event_handler_unregister(SC_EVENT, ESP_EVENT_ANY_ID, &sc_event_handler);
        wifi_config_start_ap();
        vTaskDelete(NULL);
        return;
    }
    ESP_LOGI(TAG, "smartconfig listening (%ds)...", SMARTCONFIG_TIMEOUT_S);

    EventBits_t bits = xEventGroupWaitBits(
        s_sc_events, SC_GOT_BIT, pdFALSE, pdFALSE,
        pdMS_TO_TICKS(SMARTCONFIG_TIMEOUT_S * 1000));

    esp_smartconfig_stop();
    esp_event_handler_unregister(SC_EVENT, ESP_EVENT_ANY_ID, &sc_event_handler);

    if (bits & SC_GOT_BIT) {
        ESP_LOGW(TAG, "provisioned, restarting...");
        vTaskDelay(pdMS_TO_TICKS(500));
        esp_restart();
    }

    /* 超时：切 AP 兑底（SmartConfig 已停，STA 模式可切 APSTA） */
    ESP_LOGW(TAG, "smartconfig timeout, starting provisioning AP");
    wifi_config_start_ap();
    vTaskDelete(NULL);
}

esp_err_t wifi_config_start_smartconfig(void)
{
    BaseType_t ok = xTaskCreate(smartconfig_task, "smartconfig", 4096, NULL, 5, NULL);
    return ok == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}

/* ---------------- 配网页 ---------------- */

static const char AP_PAGE_HTML[] =
    "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\">"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<title>FramedPhoto 配网</title><style>"
    "body{font-family:-apple-system,'PingFang SC',sans-serif;background:#f5f6f8;margin:0;padding:24px}"
    "h1{font-size:20px}label{display:block;font-size:14px;margin:14px 0 4px}"
    "input,select{width:100%;padding:10px;border:1px solid #d1d5db;border-radius:8px;box-sizing:border-box;font-size:14px}"
    "button{margin-top:18px;width:100%;padding:12px;background:#2563eb;color:#fff;border:0;border-radius:8px;font-size:15px}"
    ".hint{font-size:12px;color:#9ca3af;margin-top:6px}"
    "</style></head><body>"
    "<h1>🖼 FramedPhoto 配网</h1>"
    "<p style=\"font-size:13px;color:#374151\">选择或输入家中的 WiFi，保存后设备将自动重启连接。</p>"
    "<label>附近 WiFi</label>"
    "<select id=\"scan\" onchange=\"document.getElementById('ssid').value=this.value\">"
    "<option value=\"\">— 点击刷新扫描 —</option></select>"
    "<button onclick=\"scanWifi()\" style=\"margin-top:8px;background:#374151\">扫描附近 WiFi</button>"
    "<label>SSID</label><input id=\"ssid\" placeholder=\"WiFi 名称\">"
    "<label>密码</label><input id=\"pass\" type=\"password\" placeholder=\"WiFi 密码\">"
    "<button onclick=\"save()\">保存并连接</button>"
    "<div id=\"msg\" class=\"hint\"></div>"
    "<script>"
    "async function scanWifi(){"
    "const m=document.getElementById('msg');m.textContent='扫描中…';"
    "const r=await fetch('/api/scan');const j=await r.json();"
    "const sel=document.getElementById('scan');sel.innerHTML='<option value=\"\">— 选择 —</option>';"
    "(j.aps||[]).forEach(a=>{const o=document.createElement('option');o.value=a.ssid;o.textContent=a.ssid;sel.appendChild(o);});"
    "m.textContent='找到 '+(j.aps||[]).length+' 个 WiFi';}"
    "async function save(){"
    "const m=document.getElementById('msg');m.textContent='保存中…';"
    "const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({ssid:document.getElementById('ssid').value.trim(),"
    "password:document.getElementById('pass').value})});"
    "const j=await r.json();"
    "if(r.ok){m.textContent='保存成功，设备正在重启并连接…';}else{m.textContent=j.error||'保存失败';}"
    "}"
    "</script></body></html>";

/* ---------------- HTTP 接口 ---------------- */

static esp_err_t page_handler(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html");
    return httpd_resp_send(req, AP_PAGE_HTML, HTTPD_RESP_USE_STRLEN);
}

typedef struct {
    char ssid[SCAN_MAX_APS][33];
    int count;
} scan_ctx_t;

static esp_err_t scan_handler(httpd_req_t *req)
{
    wifi_scan_config_t scan_cfg = {
        .ssid = NULL,
        .bssid = NULL,
        .channel = 0,
        .show_hidden = true,
        .scan_type = WIFI_SCAN_TYPE_ACTIVE,
        .scan_time = { .active = { .min = 100, .max = 300 } },
    };
    esp_err_t err = esp_wifi_scan_start(&scan_cfg, true);
    if (err != ESP_OK) {
        httpd_resp_set_status(req, "500");
        return httpd_resp_sendstr(req, "{\"error\":\"scan failed\"}");
    }

    uint16_t n = 0;
    esp_wifi_scan_get_ap_num(&n);
    if (n > SCAN_MAX_APS) {
        n = SCAN_MAX_APS;
    }
    wifi_ap_record_t aps[SCAN_MAX_APS];
    esp_wifi_scan_get_ap_records(&n, aps);
    esp_wifi_scan_stop();

    /* 按 SSID 去重（同 SSID 多频段只保留信号最强） */
    scan_ctx_t ctx = { .count = 0 };
    for (uint16_t i = 0; i < n; i++) {
        bool dup = false;
        for (int j = 0; j < ctx.count; j++) {
            if (strcmp(ctx.ssid[j], (const char *)aps[i].ssid) == 0) {
                dup = true;
                break;
            }
        }
        if (!dup && aps[i].ssid[0] != '\0' && ctx.count < SCAN_MAX_APS) {
            strncpy(ctx.ssid[ctx.count], (const char *)aps[i].ssid, 32);
            ctx.count++;
        }
    }

    cJSON *root = cJSON_CreateObject();
    cJSON *arr = cJSON_AddArrayToObject(root, "aps");
    for (int i = 0; i < ctx.count; i++) {
        cJSON *o = cJSON_CreateObject();
        cJSON_AddStringToObject(o, "ssid", ctx.ssid[i]);
        cJSON_AddItemToArray(arr, o);
    }
    char *body = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    httpd_resp_set_type(req, "application/json");
    esp_err_t r = httpd_resp_sendstr(req, body);
    free(body);
    return r;
}

static esp_err_t config_handler(httpd_req_t *req)
{
    char buf[192];
    int total = 0;
    int ret;
    while ((ret = httpd_req_recv(req, buf + total, sizeof(buf) - 1 - total)) > 0) {
        total += ret;
        if (total >= (int)sizeof(buf) - 1) {
            break;
        }
    }
    buf[total] = '\0';

    cJSON *root = cJSON_Parse(buf);
    if (!root) {
        httpd_resp_set_status(req, "400");
        return httpd_resp_sendstr(req, "{\"error\":\"bad json\"}");
    }
    cJSON *ssid_j = cJSON_GetObjectItem(root, "ssid");
    cJSON *pass_j = cJSON_GetObjectItem(root, "password");
    const char *ssid = cJSON_IsString(ssid_j) ? ssid_j->valuestring : "";
    const char *pass = cJSON_IsString(pass_j) ? pass_j->valuestring : "";
    cJSON_Delete(root);

    if (strlen(ssid) == 0 || strlen(ssid) >= 32) {
        httpd_resp_set_status(req, "400");
        return httpd_resp_sendstr(req, "{\"error\":\"ssid required (<=32 chars)\"}");
    }

    esp_err_t err = wifi_app_save_credentials(ssid, pass);
    if (err != ESP_OK) {
        httpd_resp_set_status(req, "500");
        return httpd_resp_sendstr(req, "{\"error\":\"nvs save failed\"}");
    }
    ESP_LOGI(TAG, "config saved, restarting...");
    httpd_resp_sendstr(req, "{\"ok\":true}");

    /* 延时重启，让响应先发出去 */
    vTaskDelay(pdMS_TO_TICKS(800));
    esp_restart();
    return ESP_OK; /* 不会到达 */
}

/* ---------------- 启动 ---------------- */

esp_err_t wifi_config_start_ap(void)
{
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_SOFTAP);
    snprintf(s_ap_ssid, sizeof(s_ap_ssid), "FramedPhoto-%02X%02X%02X", mac[3], mac[4], mac[5]);

    esp_netif_t *ap = esp_netif_create_default_wifi_ap();
    if (!ap) {
        ESP_LOGE(TAG, "create ap netif failed");
        return ESP_FAIL;
    }

    wifi_config_t wc = {
        .ap = {
            .ssid_len = 0,
            .channel = 1,
            .max_connection = 4,
            .authmode = WIFI_AUTH_OPEN,
        },
    };
    strncpy((char *)wc.ap.ssid, s_ap_ssid, sizeof(wc.ap.ssid) - 1);

    /* APSTA 共存：配网页需扫描附近 WiFi（扫描 API 要求 STA 模式） */
    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_APSTA), TAG, "set apsta mode");
    ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_AP, &wc), TAG, "set ap config");
    ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "ap start");

    httpd_handle_t server = NULL;
    httpd_config_t hcfg = HTTPD_DEFAULT_CONFIG();
    hcfg.uri_match_fn = httpd_uri_match_wildcard;
    ESP_RETURN_ON_ERROR(httpd_start(&server, &hcfg), TAG, "httpd start");

    httpd_uri_t uri_page = { .uri = "/", .method = HTTP_GET, .handler = page_handler, .user_ctx = NULL };
    httpd_uri_t uri_scan = { .uri = "/api/scan", .method = HTTP_GET, .handler = scan_handler, .user_ctx = NULL };
    httpd_uri_t uri_cfg  = { .uri = "/api/config", .method = HTTP_POST, .handler = config_handler, .user_ctx = NULL };
    httpd_register_uri_handler(server, &uri_page);
    httpd_register_uri_handler(server, &uri_scan);
    httpd_register_uri_handler(server, &uri_cfg);

    ESP_LOGI(TAG, "provisioning AP '%s' at 192.168.4.1", s_ap_ssid);
    return ESP_OK;
}
