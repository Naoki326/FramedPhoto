/*
 * gdey_epd.c — 通用 EPD 底层：SPI 总线（8-bit 命令相位）、GPIO 片选、收发
 */
#include <string.h>
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "gdey_epd.h"

static const char *TAG = "gdey_epd";

#define EPD_RESET_HOLD_MS    10
#define EPD_RESET_RELEASE_MS 20
#define DATA_CHUNK_MAX       4096

esp_err_t gdey_epd_bus_init(gdey_epd_dev_t *dev)
{
    if (dev == NULL || dev->cs_count == 0 || dev->cs_count > GDEY_EPD_MAX_CS) {
        return ESP_ERR_INVALID_ARG;
    }

    /* 控制引脚：CS* / RST / LOAD_SW 输出，BUSY 输入 */
    for (uint8_t i = 0; i < dev->cs_count; i++) {
        ESP_RETURN_ON_ERROR(gpio_set_direction(dev->cs[i], GPIO_MODE_OUTPUT), TAG, "cs%d dir", i);
        ESP_RETURN_ON_ERROR(gpio_set_level(dev->cs[i], 1), TAG, "cs%d level", i);
    }
    if (dev->rst != GPIO_NUM_NC) {
        ESP_RETURN_ON_ERROR(gpio_set_direction(dev->rst, GPIO_MODE_OUTPUT), TAG, "rst dir");
        gpio_set_level(dev->rst, 1);
    }
    if (dev->load_sw != GPIO_NUM_NC) {
        ESP_RETURN_ON_ERROR(gpio_set_direction(dev->load_sw, GPIO_MODE_OUTPUT), TAG, "load_sw dir");
        ESP_RETURN_ON_ERROR(gpio_set_level(dev->load_sw, 1), TAG, "load_sw level");
    }
    if (dev->busy != GPIO_NUM_NC) {
        ESP_RETURN_ON_ERROR(gpio_set_direction(dev->busy, GPIO_MODE_INPUT), TAG, "busy dir");
    }

    /* SPI 总线（官方示例：SPI3_HOST，10MHz，8-bit 命令相位） */
    spi_bus_config_t buscfg = {
        .mosi_io_num = CONFIG_GDEY_EPD_SPI_MOSI,
        .miso_io_num = -1,
        .sclk_io_num = CONFIG_GDEY_EPD_SPI_SCLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = DATA_CHUNK_MAX * 2,
    };
    ESP_RETURN_ON_ERROR(spi_bus_initialize(dev->host, &buscfg, SPI_DMA_CH_AUTO),
                        TAG, "spi bus init failed");

    spi_device_interface_config_t devcfg = {
        .command_bits = 8,           /* 8-bit 命令相位（trans.cmd） */
        .clock_speed_hz = dev->clock_hz,
        .mode = 0,
        .spics_io_num = -1,          /* CS 手动 GPIO 控制 */
        .cs_ena_posttrans = 3,
        .queue_size = 4,
    };
    ESP_RETURN_ON_ERROR(spi_bus_add_device(dev->host, &devcfg, &dev->spi),
                        TAG, "spi add device failed");

    ESP_LOGI(TAG, "SPI ready: host=%d clock=%dHz cs=%u", dev->host, dev->clock_hz, dev->cs_count);
    return ESP_OK;
}

esp_err_t gdey_epd_hw_reset(gdey_epd_dev_t *dev)
{
    if (dev->rst == GPIO_NUM_NC) {
        return ESP_OK;
    }
    gpio_set_level(dev->rst, 0);
    vTaskDelay(pdMS_TO_TICKS(EPD_RESET_HOLD_MS));
    gpio_set_level(dev->rst, 1);
    vTaskDelay(pdMS_TO_TICKS(EPD_RESET_RELEASE_MS));
    return ESP_OK;
}

esp_err_t gdey_epd_wait_busy(gdey_epd_dev_t *dev, uint32_t timeout_ms)
{
    if (dev->busy == GPIO_NUM_NC) {
        vTaskDelay(pdMS_TO_TICKS(100)); /* 无 BUSY 时保守延时 */
        return ESP_OK;
    }
    TickType_t start = xTaskGetTickCount();
    while (gpio_get_level(dev->busy) == 1) { /* 高 = 忙 */
        if ((xTaskGetTickCount() - start) > pdMS_TO_TICKS(timeout_ms)) {
            ESP_LOGW(TAG, "wait busy timeout (%lu ms)", (unsigned long)timeout_ms);
            return ESP_ERR_TIMEOUT;
        }
        vTaskDelay(pdMS_TO_TICKS(5));
    }
    return ESP_OK;
}

esp_err_t gdey_epd_select(gdey_epd_dev_t *dev, uint8_t idx, bool active)
{
    if (idx >= dev->cs_count) {
        return ESP_ERR_INVALID_ARG;
    }
    return gpio_set_level(dev->cs[idx], active ? 0 : 1);
}

esp_err_t gdey_epd_select_all(gdey_epd_dev_t *dev, bool active)
{
    for (uint8_t i = 0; i < dev->cs_count; i++) {
        ESP_RETURN_ON_ERROR(gpio_set_level(dev->cs[i], active ? 0 : 1), TAG, "cs%d", i);
    }
    return ESP_OK;
}

esp_err_t gdey_epd_cmd(gdey_epd_dev_t *dev, uint8_t cmd)
{
    spi_transaction_t t = {
        .cmd = cmd,
        .length = 0,
        .tx_buffer = NULL,
    };
    ESP_RETURN_ON_ERROR(spi_device_transmit(dev->spi, &t), TAG, "cmd 0x%02x", cmd);
    return ESP_OK;
}

esp_err_t gdey_epd_cmd_data(gdey_epd_dev_t *dev, uint8_t cmd, const uint8_t *data, size_t len)
{
    if (data == NULL || len == 0) {
        return gdey_epd_cmd(dev, cmd);
    }
    if (len > DATA_CHUNK_MAX) {
        ESP_LOGE(TAG, "cmd_data too long: %u", (unsigned)len);
        return ESP_ERR_INVALID_SIZE;
    }
    spi_transaction_t t = {
        .cmd = cmd,
        .length = len * 8,
        .tx_buffer = data,
    };
    ESP_RETURN_ON_ERROR(spi_device_transmit(dev->spi, &t), TAG, "cmd 0x%02x data", cmd);
    return ESP_OK;
}

esp_err_t gdey_epd_data(gdey_epd_dev_t *dev, const uint8_t *data, size_t len)
{
    while (len > 0) {
        size_t chunk = len > DATA_CHUNK_MAX ? DATA_CHUNK_MAX : len;
        /* 可变命令相位：command_bits=0 时省略命令字节（同官方 spiTransmitData） */
        spi_transaction_ext_t t = {
            .command_bits = 0,
            .base.length = chunk * 8,
            .base.tx_buffer = data,
            .base.flags = SPI_TRANS_VARIABLE_CMD,
        };
        ESP_RETURN_ON_ERROR(spi_device_transmit(dev->spi, (spi_transaction_t *)&t), TAG, "data");
        data += chunk;
        len -= chunk;
    }
    return ESP_OK;
}
