/*
 * gdey_epd.c — 通用 EPD 底层：SPI 总线、引脚时序、命令/数据收发
 */
#include <string.h>
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "gdey_epd.h"

static const char *TAG = "gdey_epd";

#define EPD_RESET_HOLD_MS   10
#define EPD_RESET_RELEASE_MS 20

esp_err_t gdey_epd_bus_init(gdey_epd_dev_t *dev, int clock_hz)
{
    if (dev == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    // 初始化控制引脚
    const gpio_num_t pins[] = { dev->pin_cs, dev->pin_dc, dev->pin_rst, dev->pin_busy, dev->pin_pwr };
    for (size_t i = 0; i < sizeof(pins) / sizeof(pins[0]); i++) {
        if (pins[i] == GPIO_NUM_NC) {
            continue;
        }
        gpio_config_t io = {
            .pin_bit_mask = 1ULL << pins[i],
            .mode = GPIO_MODE_OUTPUT,
            .pull_up_en = GPIO_PULLUP_DISABLE,
            .pull_down_en = GPIO_PULLDOWN_DISABLE,
            .intr_type = GPIO_INTR_DISABLE,
        };
        // BUSY 是输入脚
        if (pins[i] == dev->pin_busy) {
            io.mode = GPIO_MODE_INPUT;
            io.pull_up_en = GPIO_PULLUP_ENABLE;
        }
        ESP_RETURN_ON_ERROR(gpio_config(&io), TAG, "gpio config pin %d failed", pins[i]);
        if (pins[i] != dev->pin_busy) {
            gpio_set_level(pins[i], 1);
        }
    }

    // 配置 SPI 总线
    spi_bus_config_t buscfg = {
        .mosi_io_num = CONFIG_GDEY_EPD_SPI_MOSI,
        .miso_io_num = -1,
        .sclk_io_num = CONFIG_GDEY_EPD_SPI_SCLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 1200 * 4, /* 单行最大传输（预留） */
    };
    ESP_RETURN_ON_ERROR(
        spi_bus_initialize(dev->host, &buscfg, SPI_DMA_CH_AUTO),
        TAG, "spi bus init failed");

    spi_device_interface_config_t devcfg = {
        .clock_speed_hz = clock_hz,
        .mode = 0,
        .spics_io_num = dev->pin_cs,
        .queue_size = 4,
    };
    ESP_RETURN_ON_ERROR(
        spi_bus_add_device(dev->host, &devcfg, &dev->spi_dev),
        TAG, "spi add device failed");

    ESP_LOGI(TAG, "SPI bus ready: host=%d clock=%dHz", dev->host, clock_hz);
    return ESP_OK;
}

esp_err_t gdey_epd_hw_reset(gdey_epd_dev_t *dev)
{
    if (dev == NULL || !dev->hw_reset || dev->pin_rst == GPIO_NUM_NC) {
        return ESP_OK; // 无复位脚则跳过
    }
    gpio_set_level(dev->pin_rst, 0);
    vTaskDelay(pdMS_TO_TICKS(EPD_RESET_HOLD_MS));
    gpio_set_level(dev->pin_rst, 1);
    vTaskDelay(pdMS_TO_TICKS(EPD_RESET_RELEASE_MS));
    return ESP_OK;
}

esp_err_t gdey_epd_wait_busy(gdey_epd_dev_t *dev, uint32_t timeout_ms)
{
    if (dev->pin_busy == GPIO_NUM_NC) {
        vTaskDelay(pdMS_TO_TICKS(100)); // 无 BUSY 脚时保守延时
        return ESP_OK;
    }
    TickType_t start = xTaskGetTickCount();
    while (gpio_get_level(dev->pin_busy) == 1) { // 高电平 = 忙（常见约定）
        if ((xTaskGetTickCount() - start) > pdMS_TO_TICKS(timeout_ms)) {
            ESP_LOGW(TAG, "wait busy timeout (%d ms)", timeout_ms);
            return ESP_ERR_TIMEOUT;
        }
        vTaskDelay(pdMS_TO_TICKS(5));
    }
    return ESP_OK;
}

esp_err_t gdey_epd_send_cmd(gdey_epd_dev_t *dev, uint8_t cmd)
{
    gpio_set_level(dev->pin_dc, 0); // 命令
    spi_transaction_t t = {
        .length = 8,
        .tx_buffer = &cmd,
    };
    ESP_RETURN_ON_ERROR(
        spi_device_transmit(dev->spi_dev, &t),
        TAG, "send cmd 0x%02x failed", cmd);
    return ESP_OK;
}

esp_err_t gdey_epd_send_data(gdey_epd_dev_t *dev, const uint8_t *data, size_t len)
{
    gpio_set_level(dev->pin_dc, 1); // 数据
    while (len > 0) {
        size_t chunk = len > 4096 ? 4096 : len;
        spi_transaction_t t = {
            .length = chunk * 8,
            .tx_buffer = data,
        };
        ESP_RETURN_ON_ERROR(
            spi_device_transmit(dev->spi_dev, &t),
            TAG, "send data failed");
        data += chunk;
        len -= chunk;
    }
    return ESP_OK;
}
