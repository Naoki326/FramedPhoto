/*
 * gdey_epd.h — 好视达 (Good Display) 电子纸驱动通用接口
 *
 * 硬件事实（来自官方 ESP32 示例 + 真机验证））：
 *  - GDEB0709E01 由 2 颗 GDEP133C02 驱动 IC 拼接：CS0=左半屏、CS1=右半屏
 *  - SPI 8-bit 命令相位（trans.cmd），CS 由 GPIO 手动控制（无 D/C 引脚）
 *  - 像素格式 4bit/像素（1 字节 2 像素），高 nibble 为左像素
 *
 * 图像行布局（每行 600 字节 = 1200 像素 @ 2px/byte）：
 *    前 300B 送 IC0（屏幕左半），后 300B 送 IC1（屏幕右半）
 *    行内像素从左到右：字节 k 对应逻辑像素 (2k, 2k+1)
 *   —— 与服务端 FPS6 格式完全一致，设备端直接转发即可。
 */
#pragma once

#include <stdint.h>
#include <stddef.h>
#include "esp_err.h"
#include "driver/spi_master.h"
#include "driver/gpio.h"

#ifdef __cplusplus
extern "C" {
#endif

/* EPD 6 色 nibble 编码（与官方驱动一致） */
#define EPD_COLOR_BLACK   0x0
#define EPD_COLOR_WHITE   0x1
#define EPD_COLOR_YELLOW  0x2
#define EPD_COLOR_RED     0x3
#define EPD_COLOR_BLUE    0x5
#define EPD_COLOR_GREEN   0x6

/** 单像素字节（两像素拼一个字节）的纯色值，如 0x11=白 0x22=黄 0x33=红 */
#define EPD_COLOR_BYTE(c) ((c) << 4 | (c))

#define GDEY_EPD_MAX_CS 2

#define EPD_BUSY_TIMEOUT_MS 120000
#define EPD_ROW_DELAY_MS    1   /* 官方示例：每行发送后延时，防硬件过载 */

typedef struct {
    spi_host_device_t host;              /**< SPI host */
    gpio_num_t        cs[GDEY_EPD_MAX_CS]; /**< 各驱动 IC 片选（手动 GPIO） */
    uint8_t           cs_count;          /**< 驱动 IC 数量（拼接屏为 2） */
    gpio_num_t        rst;               /**< 复位，低有效 */
    gpio_num_t        busy;              /**< 忙指示（低=忙，BUSYN），GPIO_NUM_NC 跳过 */
    gpio_num_t        load_sw;           /**< 负载开关，GPIO_NUM_NC 跳过 */
    int               clock_hz;

    /* 型号参数（由具体型号 create 填充） */
    int width;                           /**< 屏幕总宽（像素） */
    int height;                          /**< 屏幕总高（像素） */
    int ic_width;                        /**< 单个 IC 宽（像素） */
    int bytes_per_row;                   /**< 每行总字节数（宽/2） */
    int bytes_per_ic_row;                /**< 单个 IC 每行字节数（宽/2/IC 数） */

    /* 内部状态 */
    spi_device_handle_t spi;
} gdey_epd_dev_t;

/* ---------- 底层：总线 / GPIO / 收发（型号驱动用） ---------- */

esp_err_t gdey_epd_bus_init(gdey_epd_dev_t *dev);

esp_err_t gdey_epd_hw_reset(gdey_epd_dev_t *dev);

esp_err_t gdey_epd_wait_busy(gdey_epd_dev_t *dev, uint32_t timeout_ms);

/** 选中/释放单个 IC（CS 拉低/拉高），idx < cs_count */
esp_err_t gdey_epd_select(gdey_epd_dev_t *dev, uint8_t idx, bool active);

/** 同时选中/释放全部 IC */
esp_err_t gdey_epd_select_all(gdey_epd_dev_t *dev, bool active);

/** 发送单条命令（无数据，8-bit 命令相位） */
esp_err_t gdey_epd_cmd(gdey_epd_dev_t *dev, uint8_t cmd);

/** 发送命令 + 数据（一次 CS 周期内） */
esp_err_t gdey_epd_cmd_data(gdey_epd_dev_t *dev, uint8_t cmd, const uint8_t *data, size_t len);

/** 仅发送数据（大数据自动分块） */
esp_err_t gdey_epd_data(gdey_epd_dev_t *dev, const uint8_t *data, size_t len);

#ifdef __cplusplus
}
#endif
