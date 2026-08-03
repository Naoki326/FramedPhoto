/*
 * gdeb0709e01.c — GDEB0709E01 (E Ink Spectra 6, 1200x1600, 双 GDEP133C02 IC) 驱动
 *
 * 命令序列与刷图流程移植自官方 ESP32 示例
 * （docs/vendor/gdeb0709e01/esp32_example/GDEB0709E01/main/GDEP133C02.c）。
 *
 * 官方代码要点：
 *  - init 序列中参数类命令写双 CS，电源类命令只写 CS0（主 IC）
 *  - 刷图：先 IC0 逐行 300B（右半屏），再 IC1 逐行 300B（左半屏），每行延时 1ms
 *  - 刷新：PON -> (30ms) -> DRF -> POF，每步 BUSY 同步
 */
#include <string.h>
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "gdeb0709e01.h"

static const char *TAG = "gdeb0709e01";

/* ---------------- 命令码（官方 GDEP133C02.h） ---------------- */
#define CMD_PSR     0x00
#define CMD_PWR     0x01
#define CMD_POF     0x02
#define CMD_PON     0x04
#define CMD_BTST_N  0x05
#define CMD_BTST_P  0x06
#define CMD_DTM     0x10
#define CMD_DRF     0x12
#define CMD_CDI     0x50
#define CMD_TCON    0x60
#define CMD_TRES    0x61
#define CMD_AN_TM   0x74
#define CMD_AGID    0x86
#define CMD_BUCK_BOOST_VDDN 0xB0
#define CMD_TFT_VCOM_POWER  0xB1
#define CMD_EN_BUF  0xB6
#define CMD_BOOST_VDDP_EN   0xB7
#define CMD_CCSET   0xE0
#define CMD_PWS     0xE3
#define CMD_CMD66   0xF0

/* ---------------- 命令参数（官方 GDEP133C02.c） ---------------- */
static const uint8_t PSR_V[2]           = { 0xDF, 0x69 };
static const uint8_t PWR_V[6]           = { 0x0F, 0x00, 0x28, 0x2C, 0x28, 0x38 };
static const uint8_t POF_V[1]           = { 0x00 };
static const uint8_t DRF_V[1]           = { 0x01 };
static const uint8_t CDI_V[1]           = { 0xF7 };
static const uint8_t TCON_V[2]          = { 0x03, 0x03 };
static const uint8_t TRES_V[4]          = { 0x04, 0xB0, 0x03, 0x20 };
static const uint8_t CMD66_V[6]         = { 0x49, 0x55, 0x13, 0x5D, 0x05, 0x10 };
static const uint8_t EN_BUF_V[1]        = { 0x07 };
static const uint8_t CCSET_V[1]         = { 0x01 };
static const uint8_t PWS_V[1]           = { 0x22 };
static const uint8_t AN_TM_V[9]         = { 0xC0, 0x1E, 0x1E, 0xCE, 0xCE, 0xCE, 0x15, 0x15, 0x55 };
static const uint8_t AGID_V[1]          = { 0x10 };
static const uint8_t BTST_P_V[2]        = { 0xE8, 0x28 };
static const uint8_t BOOST_VDDP_EN_V[1] = { 0x01 };
static const uint8_t BTST_N_V[2]        = { 0xE8, 0x28 };
static const uint8_t BUCK_BOOST_VDDN_V[1] = { 0x01 };
static const uint8_t TFT_VCOM_POWER_V[1]  = { 0x02 };

/* 全屏填充用小块缓冲（官方 8192，逐块写，无需整帧 RAM） */
#define FILL_BUF_SIZE 8192

esp_err_t gdeb0709e01_create(gdey_epd_dev_t *dev)
{
    if (dev == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    memset(dev, 0, sizeof(*dev));
    dev->host       = CONFIG_GDEY_EPD_SPI_HOST;
    dev->cs[0]      = CONFIG_GDEY_EPD_PIN_CS0;
    dev->cs[1]      = CONFIG_GDEY_EPD_PIN_CS1;
    dev->cs_count   = 2;
    dev->rst        = CONFIG_GDEY_EPD_PIN_RST;
    dev->busy       = CONFIG_GDEY_EPD_PIN_BUSY;
    dev->load_sw    = CONFIG_GDEY_EPD_PIN_LOAD_SW;
    dev->clock_hz   = CONFIG_GDEY_EPD_SPI_CLOCK_HZ;

    dev->width         = GDEB0709E01_WIDTH;
    dev->height        = GDEB0709E01_HEIGHT;
    dev->ic_width      = GDEB0709E01_WIDTH / 2;
    dev->bytes_per_row = GDEB0709E01_WIDTH / 2;
    dev->bytes_per_ic_row = dev->bytes_per_row / 2;

    ESP_RETURN_ON_ERROR(gdey_epd_bus_init(dev), TAG, "bus init");
    ESP_RETURN_ON_ERROR(gdey_epd_hw_reset(dev), TAG, "hw reset");

    ESP_LOGI(TAG, "ready: %dx%d, 2x IC@%dpx, SPI%u @%dHz",
             dev->width, dev->height, dev->ic_width, dev->host, dev->clock_hz);
    return ESP_OK;
}

/* 参数类命令：双 CS（官方 initEPD 多数命令） */
static esp_err_t wr_cmd_all(gdey_epd_dev_t *dev, uint8_t cmd, const uint8_t *data, size_t len)
{
    ESP_RETURN_ON_ERROR(gdey_epd_select_all(dev, true), TAG, "sel all");
    esp_err_t err = gdey_epd_cmd_data(dev, cmd, data, len);
    ESP_RETURN_ON_ERROR(gdey_epd_select_all(dev, false), TAG, "desel all");
    return err;
}

/* 电源类命令：只写 CS0（主 IC） */
static esp_err_t wr_cmd_ic0(gdey_epd_dev_t *dev, uint8_t cmd, const uint8_t *data, size_t len)
{
    ESP_RETURN_ON_ERROR(gdey_epd_select(dev, 0, true), TAG, "sel ic0");
    esp_err_t err = gdey_epd_cmd_data(dev, cmd, data, len);
    ESP_RETURN_ON_ERROR(gdey_epd_select(dev, 0, false), TAG, "desel ic0");
    return err;
}

esp_err_t gdeb0709e01_init(gdey_epd_dev_t *dev)
{
    ESP_RETURN_ON_ERROR(gdey_epd_hw_reset(dev), TAG, "reset");
    ESP_RETURN_ON_ERROR(gdey_epd_wait_busy(dev, EPD_BUSY_TIMEOUT_MS), TAG, "busy after reset");

    ESP_RETURN_ON_ERROR(wr_cmd_ic0(dev, CMD_AN_TM, AN_TM_V, sizeof(AN_TM_V)), TAG, "AN_TM");
    ESP_RETURN_ON_ERROR(wr_cmd_all(dev, CMD_CMD66, CMD66_V, sizeof(CMD66_V)), TAG, "CMD66");
    ESP_RETURN_ON_ERROR(wr_cmd_all(dev, CMD_PSR,  PSR_V,  sizeof(PSR_V)),  TAG, "PSR");
    ESP_RETURN_ON_ERROR(wr_cmd_all(dev, CMD_CDI,  CDI_V,  sizeof(CDI_V)),  TAG, "CDI");
    ESP_RETURN_ON_ERROR(wr_cmd_all(dev, CMD_TCON, TCON_V, sizeof(TCON_V)), TAG, "TCON");
    ESP_RETURN_ON_ERROR(wr_cmd_all(dev, CMD_AGID, AGID_V, sizeof(AGID_V)), TAG, "AGID");
    ESP_RETURN_ON_ERROR(wr_cmd_all(dev, CMD_PWS,  PWS_V,  sizeof(PWS_V)),  TAG, "PWS");
    ESP_RETURN_ON_ERROR(wr_cmd_all(dev, CMD_CCSET, CCSET_V, sizeof(CCSET_V)), TAG, "CCSET");
    ESP_RETURN_ON_ERROR(wr_cmd_all(dev, CMD_TRES, TRES_V, sizeof(TRES_V)), TAG, "TRES");

    ESP_RETURN_ON_ERROR(wr_cmd_ic0(dev, CMD_PWR,   PWR_V,   sizeof(PWR_V)),   TAG, "PWR");
    ESP_RETURN_ON_ERROR(wr_cmd_ic0(dev, CMD_EN_BUF, EN_BUF_V, sizeof(EN_BUF_V)), TAG, "EN_BUF");
    ESP_RETURN_ON_ERROR(wr_cmd_ic0(dev, CMD_BTST_P, BTST_P_V, sizeof(BTST_P_V)), TAG, "BTST_P");
    ESP_RETURN_ON_ERROR(wr_cmd_ic0(dev, CMD_BOOST_VDDP_EN, BOOST_VDDP_EN_V, sizeof(BOOST_VDDP_EN_V)), TAG, "BOOST_VDDP_EN");
    ESP_RETURN_ON_ERROR(wr_cmd_ic0(dev, CMD_BTST_N, BTST_N_V, sizeof(BTST_N_V)), TAG, "BTST_N");
    ESP_RETURN_ON_ERROR(wr_cmd_ic0(dev, CMD_BUCK_BOOST_VDDN, BUCK_BOOST_VDDN_V, sizeof(BUCK_BOOST_VDDN_V)), TAG, "BUCK_BOOST_VDDN");
    ESP_RETURN_ON_ERROR(wr_cmd_ic0(dev, CMD_TFT_VCOM_POWER, TFT_VCOM_POWER_V, sizeof(TFT_VCOM_POWER_V)), TAG, "TFT_VCOM_POWER");

    ESP_LOGI(TAG, "init sequence done");
    return ESP_OK;
}

esp_err_t gdeb0709e01_refresh(gdey_epd_dev_t *dev)
{
    /* PON */
    ESP_RETURN_ON_ERROR(gdey_epd_select_all(dev, true), TAG, "pon sel");
    ESP_RETURN_ON_ERROR(gdey_epd_cmd(dev, CMD_PON), TAG, "PON");
    ESP_RETURN_ON_ERROR(gdey_epd_wait_busy(dev, EPD_BUSY_TIMEOUT_MS), TAG, "busy PON");
    ESP_RETURN_ON_ERROR(gdey_epd_select_all(dev, false), TAG, "pon desel");

    vTaskDelay(pdMS_TO_TICKS(30));

    /* DRF */
    ESP_RETURN_ON_ERROR(gdey_epd_select_all(dev, true), TAG, "drf sel");
    ESP_RETURN_ON_ERROR(gdey_epd_cmd_data(dev, CMD_DRF, DRF_V, sizeof(DRF_V)), TAG, "DRF");
    ESP_RETURN_ON_ERROR(gdey_epd_wait_busy(dev, EPD_BUSY_TIMEOUT_MS), TAG, "busy DRF");
    ESP_RETURN_ON_ERROR(gdey_epd_select_all(dev, false), TAG, "drf desel");

    /* POF */
    ESP_RETURN_ON_ERROR(gdey_epd_select_all(dev, true), TAG, "pof sel");
    ESP_RETURN_ON_ERROR(gdey_epd_cmd_data(dev, CMD_POF, POF_V, sizeof(POF_V)), TAG, "POF");
    ESP_RETURN_ON_ERROR(gdey_epd_wait_busy(dev, EPD_BUSY_TIMEOUT_MS), TAG, "busy POF");
    ESP_RETURN_ON_ERROR(gdey_epd_select_all(dev, false), TAG, "pof desel");

    ESP_LOGI(TAG, "refresh done");
    return ESP_OK;
}

esp_err_t gdeb0709e01_fill(gdey_epd_dev_t *dev, uint8_t color_byte)
{
    /* 官方 epdDisplayColor：DTM + 每 IC 480000B（300B*1600 行） */
    static uint8_t buf[FILL_BUF_SIZE];
    memset(buf, color_byte, sizeof(buf));

    ESP_RETURN_ON_ERROR(gdey_epd_select_all(dev, true), TAG, "fill sel");
    ESP_RETURN_ON_ERROR(gdey_epd_cmd(dev, CMD_DTM), TAG, "DTM");
    for (uint32_t sent = 0; sent < GDEB0709E01_IC_FRAME_SIZE; sent += FILL_BUF_SIZE) {
        uint32_t chunk = (GDEB0709E01_IC_FRAME_SIZE - sent) > FILL_BUF_SIZE
                             ? FILL_BUF_SIZE : (GDEB0709E01_IC_FRAME_SIZE - sent);
        ESP_RETURN_ON_ERROR(gdey_epd_data(dev, buf, chunk), TAG, "fill data");
    }
    ESP_RETURN_ON_ERROR(gdey_epd_select_all(dev, false), TAG, "fill desel");

    ESP_LOGI(TAG, "fill 0x%02x", color_byte);
    return gdeb0709e01_refresh(dev);
}

esp_err_t gdeb0709e01_begin(gdey_epd_dev_t *dev)
{
    /* 官方 pic_display_test：先 IC0（右半屏） */
    ESP_RETURN_ON_ERROR(gdey_epd_select(dev, 0, true), TAG, "begin sel ic0");
    ESP_RETURN_ON_ERROR(gdey_epd_cmd(dev, CMD_DTM), TAG, "begin DTM");
    return ESP_OK;
}

esp_err_t gdeb0709e01_send_ic_row(gdey_epd_dev_t *dev, const uint8_t *half_row)
{
    ESP_RETURN_ON_ERROR(gdey_epd_data(dev, half_row, dev->bytes_per_ic_row), TAG, "ic row");
    vTaskDelay(pdMS_TO_TICKS(EPD_ROW_DELAY_MS)); /* 官方：每行 1ms 防硬件过载 */
    return ESP_OK;
}

esp_err_t gdeb0709e01_next_ic(gdey_epd_dev_t *dev)
{
    ESP_RETURN_ON_ERROR(gdey_epd_select(dev, 0, false), TAG, "next desel ic0");
    ESP_RETURN_ON_ERROR(gdey_epd_select(dev, 1, true), TAG, "next sel ic1");
    ESP_RETURN_ON_ERROR(gdey_epd_cmd(dev, CMD_DTM), TAG, "next DTM");
    return ESP_OK;
}

esp_err_t gdeb0709e01_end(gdey_epd_dev_t *dev)
{
    ESP_RETURN_ON_ERROR(gdey_epd_select(dev, 1, false), TAG, "end desel ic1");
    return gdeb0709e01_refresh(dev);
}

esp_err_t gdeb0709e01_display_image(gdey_epd_dev_t *dev, const uint8_t *frame, size_t len)
{
    if (frame == NULL || len < GDEB0709E01_FRAME_SIZE) {
        return ESP_ERR_INVALID_ARG;
    }
    const size_t half = dev->bytes_per_ic_row; /* 300 */

    /* 阶段 1：IC0 全部行（row 前 300B） */
    ESP_RETURN_ON_ERROR(gdeb0709e01_begin(dev), TAG, "begin");
    for (int y = 0; y < dev->height; y++) {
        ESP_RETURN_ON_ERROR(gdeb0709e01_send_ic_row(dev, frame + (size_t)y * dev->bytes_per_row),
                            TAG, "ic0 row %d", y);
    }
    /* 阶段 2：IC1 全部行（row 后 300B） */
    ESP_RETURN_ON_ERROR(gdeb0709e01_next_ic(dev), TAG, "next_ic");
    for (int y = 0; y < dev->height; y++) {
        ESP_RETURN_ON_ERROR(gdeb0709e01_send_ic_row(dev, frame + (size_t)y * dev->bytes_per_row + half),
                            TAG, "ic1 row %d", y);
    }
    return gdeb0709e01_end(dev);
}
