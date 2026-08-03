/*
 * display.c — 显示调度任务
 *
 * 当前为骨架：打印驱动状态并周期性占位。
 * TODO(M2): HTTP 拉取内容清单 + 图片解码 + gdey_epd 渲染 + 刷新。
 */
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "gdeb0709e01.h"
#include "display.h"

static const char *TAG = "display";

static void display_task(void *arg)
{
    gdey_epd_dev_t *epd = (gdey_epd_dev_t *)arg;
    ESP_LOGI(TAG, "display task started (%dx%d @ SPI%u)", epd->width, epd->height, epd->host);

    /* M1: 初始化（命令序列待数据手册） */
    esp_err_t err = gdeb0709e01_init(epd);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "epd init failed: %s", esp_err_to_name(err));
    }

    for (;;) {
        ESP_LOGI(TAG, "heartbeat: display loop idle (poll server @ every %d min once implemented)",
                 CONFIG_FRAMEDPHOTO_POLL_INTERVAL_MIN);
        vTaskDelay(pdMS_TO_TICKS(CONFIG_FRAMEDPHOTO_POLL_INTERVAL_MIN * 60 * 1000));
    }
}

esp_err_t display_start(gdey_epd_dev_t *epd)
{
    BaseType_t ok = xTaskCreate(display_task, "display", 8192, epd, 5, NULL);
    return ok == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}
