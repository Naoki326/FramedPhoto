/*
 * gdeb0709e01.c — GDEB0709E01 (E Ink Spectra 6, 1200x1600) 驱动实现
 *
 * 状态：骨架。硬件复位与总线已打通；
 * init sequence / 清屏 / 刷图命令等待官方数据手册（roadmap M1）填充。
 */
#include <string.h>
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "gdeb0709e01.h"

static const char *TAG = "gdeb0709e01";

esp_err_t gdeb0709e01_create(gdey_epd_dev_t *dev)
{
    if (dev == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    memset(dev, 0, sizeof(*dev));
    dev->host     = CONFIG_GDEY_EPD_SPI_HOST;
    dev->pin_cs   = CONFIG_GDEY_EPD_PIN_CS;
    dev->pin_dc   = CONFIG_GDEY_EPD_PIN_DC;
    dev->pin_rst  = CONFIG_GDEY_EPD_PIN_RST;
    dev->pin_busy = CONFIG_GDEY_EPD_PIN_BUSY;
    dev->pin_pwr  = CONFIG_GDEY_EPD_PIN_PWR;
    dev->width    = GDEB0709E01_WIDTH;
    dev->height   = GDEB0709E01_HEIGHT;
    dev->hw_reset = true;

    ESP_RETURN_ON_ERROR(
        gdey_epd_bus_init(dev, CONFIG_GDEY_EPD_SPI_CLOCK_HZ),
        TAG, "bus init failed");
    ESP_RETURN_ON_ERROR(gdey_epd_hw_reset(dev), TAG, "hw reset failed");
    ESP_LOGI(TAG, "device ready: %dx%d @ SPI%u", dev->width, dev->height, dev->host);
    return ESP_OK;
}

esp_err_t gdeb0709e01_init(gdey_epd_dev_t *dev)
{
    if (dev == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    gdey_epd_hw_reset(dev);

    /*
     * TODO(M1): 填入官方数据手册的初始化序列。
     * 结构参考（命令码占位，以手册为准）：
     *   gdey_epd_send_cmd(dev, 0x00); gdey_epd_send_data(dev, ...); // panel setting
     *   gdey_epd_send_cmd(dev, 0x4E); ...  // set RAM X address
     *   gdey_epd_send_cmd(dev, 0x4F); ...  // set RAM Y address
     *   ... 彩色位平面设置（Spectra 6 为 6 色多平面结构）...
     */
    ESP_LOGW(TAG, "init sequence not yet implemented: waiting for official datasheet (roadmap M1)");

    /* 硬件连通性确认：复位后等 BUSY 释放 */
    ESP_RETURN_ON_ERROR(gdey_epd_wait_busy(dev, EPD_BUSY_TIMEOUT_MS), TAG, "busy timeout");
    return ESP_OK;
}
