/*
 * wifi_app.c — WiFi STA 连接管理
 *
 * 凭据来源优先级：NVS ("wifi" namespace) > menuconfig 默认值。
 * 无有效凭据时只初始化 WiFi 栈、不启动 STA，由 app_main 决定开配网热点。
 */
#include <string.h>
#include "esp_check.h"
#include "esp_log.h"
#include "esp_event.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "wifi_app.h"

static const char *TAG = "wifi_app";

#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1
#define WIFI_MAX_RETRY     5

static EventGroupHandle_t s_wifi_events;
static int s_retry_count;
static bool s_have_creds;

static void event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        if (s_have_creds) {
            esp_wifi_connect();
        }
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_retry_count < WIFI_MAX_RETRY) {
            s_retry_count++;
            ESP_LOGW(TAG, "disconnected, retry %d/%d", s_retry_count, WIFI_MAX_RETRY);
            esp_wifi_connect();
        } else {
            xEventGroupSetBits(s_wifi_events, WIFI_FAIL_BIT);
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *evt = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "got IP " IPSTR, IP2STR(&evt->ip_info.ip));
        s_retry_count = 0;
        xEventGroupSetBits(s_wifi_events, WIFI_CONNECTED_BIT);
    }
}

static esp_err_t load_credentials(char *ssid, size_t ssid_len, char *pass, size_t pass_len)
{
    // 1) 优先读 NVS
    nvs_handle_t h;
    if (nvs_open("wifi", NVS_READONLY, &h) == ESP_OK) {
        size_t len = ssid_len;
        if (nvs_get_str(h, "ssid", ssid, &len) == ESP_OK) {
            pass[0] = '\0';
            len = pass_len;
            nvs_get_str(h, "pass", pass, &len);
            nvs_close(h);
            ESP_LOGI(TAG, "credentials loaded from NVS");
            return ESP_OK;
        }
        nvs_close(h);
    }
    // 2) 回退到 menuconfig
    strncpy(ssid, CONFIG_FRAMEDPHOTO_WIFI_SSID, ssid_len - 1);
    strncpy(pass, CONFIG_FRAMEDPHOTO_WIFI_PASSWORD, pass_len - 1);
    return ESP_OK;
}

static bool credentials_valid(const char *ssid)
{
    return ssid[0] != '\0' && strcmp(ssid, "your_ssid") != 0;
}

bool wifi_app_has_credentials(void)
{
    char ssid[33];
    char pass[65];
    load_credentials(ssid, sizeof(ssid), pass, sizeof(pass));
    return credentials_valid(ssid);
}

esp_err_t wifi_app_save_credentials(const char *ssid, const char *pass)
{
    if (ssid == NULL || strlen(ssid) == 0 || strlen(ssid) >= 32 || strlen(pass) >= 64) {
        return ESP_ERR_INVALID_ARG;
    }
    nvs_handle_t h;
    ESP_RETURN_ON_ERROR(nvs_open("wifi", NVS_READWRITE, &h), TAG, "nvs open");
    esp_err_t err = nvs_set_str(h, "ssid", ssid);
    if (err == ESP_OK) {
        err = nvs_set_str(h, "pass", pass);
    }
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);
    ESP_RETURN_ON_ERROR(err, TAG, "save credentials");
    ESP_LOGI(TAG, "credentials saved to NVS: '%s'", ssid);
    return ESP_OK;
}

esp_err_t wifi_app_start(void)
{
    if (s_wifi_events == NULL) {
        s_wifi_events = xEventGroupCreate();
    }

    /* WiFi 栈总是初始化（STA 与配网 AP 共用） */
    ESP_RETURN_ON_ERROR(esp_netif_init(), TAG, "netif init failed");
    ESP_RETURN_ON_ERROR(esp_event_loop_create_default(), TAG, "event loop failed");
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_RETURN_ON_ERROR(esp_wifi_init(&cfg), TAG, "wifi init failed");
    ESP_RETURN_ON_ERROR(
        esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &event_handler, NULL),
        TAG, "wifi event register failed");
    ESP_RETURN_ON_ERROR(
        esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &event_handler, NULL),
        TAG, "ip event register failed");

    char ssid[33], pass[65];
    load_credentials(ssid, sizeof(ssid), pass, sizeof(pass));
    s_have_creds = credentials_valid(ssid);

    /* 无凭据时也启动 WiFi 驱动（STA 模式，不配置连接）：
       smartconfig 混杂模式嗅探需要 WiFi 已 start */
    esp_wifi_set_mode(WIFI_MODE_STA);
    if (!s_have_creds) {
        ESP_LOGW(TAG, "WiFi credentials not configured (NVS or menuconfig)");
        xEventGroupSetBits(s_wifi_events, WIFI_FAIL_BIT);
        esp_wifi_start();
        return ESP_ERR_INVALID_STATE;
    }

    wifi_config_t wc = {
        .sta = {
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };
    strncpy((char *)wc.sta.ssid, ssid, sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, pass, sizeof(wc.sta.password) - 1);

    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_STA), TAG, "set mode failed");
    ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_STA, &wc), TAG, "set config failed");
    ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "wifi start failed");
    ESP_LOGI(TAG, "WiFi start: connecting to '%s'", ssid);
    return ESP_OK;
}

esp_err_t wifi_app_wait_connected(uint32_t timeout_ms)
{
    if (s_wifi_events == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    EventBits_t bits = xEventGroupWaitBits(
        s_wifi_events, WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
        pdFALSE, pdFALSE, pdMS_TO_TICKS(timeout_ms));
    if (bits & WIFI_CONNECTED_BIT) {
        return ESP_OK;
    }
    return ESP_ERR_TIMEOUT;
}
