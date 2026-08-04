/*
 * app_main.c — FramedPhoto 固件入口
 *
 * 启动流程：
 *   1. NVS 初始化（WiFi 凭据等）
 *   2. 创建 EPD 设备（总线 + 复位）
 *   3. 启动 WiFi
 *   4. 启动显示调度任务
 */
#include <stdio.h>
#include "esp_log.h"
#include "nvs_flash.h"
#include "gdeb0709e01.h"
#include "wifi_app.h"
#include "wifi_config.h"
#include "display.h"

static const char *TAG = "app_main";

void app_main(void)
{
    ESP_LOGI(TAG, "FramedPhoto v%s starting", "0.1.0-dev");

    /* 1. NVS */
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    /* 2. EPD 设备 */
    static gdey_epd_dev_t epd;
    ESP_ERROR_CHECK(gdeb0709e01_create(&epd));

    /* 3. WiFi（失败不阻塞启动，显示任务会继续） */
    err = wifi_app_start();
    if (err == ESP_ERR_INVALID_STATE) {
        /* 无有效凭据：先 SmartConfig 监听（服务端/App 广播配网，不断网），超时后自动开 AP 兑底 */
        ESP_LOGW(TAG, "no wifi credentials, starting smartconfig provisioning");
        wifi_config_start_smartconfig();
    } else if (err != ESP_OK) {
        ESP_LOGW(TAG, "wifi start deferred: %s", esp_err_to_name(err));
    }

    /* 4. 显示调度 */
    ESP_ERROR_CHECK(display_start(&epd));

    ESP_LOGI(TAG, "system up");
}
