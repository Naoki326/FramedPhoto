/*
 * gdey_epd.h — 好视达 (Good Display) 电子纸驱动通用接口
 *
 * 所有 EPD 型号通过 gdey_epd_dev_t 描述硬件连接，实现统一的
 * 初始化 / 清屏 / 刷图 / 睡眠 流程，应用层不感知具体型号。
 *
 * 当前接入型号：GDEB0709E01（E Ink Spectra 6, 1200x1600）
 */
#pragma once

#include <stdint.h>
#include "driver/spi_master.h"
#include "driver/gpio.h"

#ifdef __cplusplus
extern "C" {
#endif

/** BUSY 等待的默认超时（ms） */
#define EPD_BUSY_TIMEOUT_MS 5000

/** EPD 硬件连接描述（引脚由 Kconfig 提供默认值，可运行时覆盖） */
typedef struct {
    spi_host_device_t host;      /**< SPI host (SPI2_HOST / SPI3_HOST) */
    gpio_num_t        pin_cs;
    gpio_num_t        pin_dc;    /**< Data/Command */
    gpio_num_t        pin_rst;
    gpio_num_t        pin_busy;
    gpio_num_t        pin_pwr;   /**< 电源控制脚，-1 表示常供电 */

    int  width;                  /**< 屏幕像素宽度 */
    int  height;                 /**< 屏幕像素高度 */
    bool hw_reset;               /**< 是否执行硬件复位 */

    /* 内部状态，应用层勿直接读写 */
    spi_device_handle_t spi_dev;
} gdey_epd_dev_t;

/** 创建具体型号的设备对象（由各型号实现提供） */
typedef struct {
    void (*fill_rect)(gdey_epd_dev_t *dev, int x, int y, int w, int h, uint32_t color);
    void (*refresh)(gdey_epd_dev_t *dev);
    void (*sleep)(gdey_epd_dev_t *dev);
} gdey_epd_ops_t;

/**
 * 初始化 SPI 总线并挂载设备（不包含任何命令序列）。
 * 调用后 dev->spi_dev 有效，可进行底层收发。
 */
esp_err_t gdey_epd_bus_init(gdey_epd_dev_t *dev, int clock_hz);

/** 硬件复位（拉低 RST 引脚 10ms 再拉高），等待 BUSY 释放 */
esp_err_t gdey_epd_hw_reset(gdey_epd_dev_t *dev);

/** 等待 BUSY 引脚释放（超时上限 ms），EPD 忙时阻塞 */
esp_err_t gdey_epd_wait_busy(gdey_epd_dev_t *dev, uint32_t timeout_ms);

/** 写一条命令（DC=0） */
esp_err_t gdey_epd_send_cmd(gdey_epd_dev_t *dev, uint8_t cmd);

/** 写数据（DC=1） */
esp_err_t gdey_epd_send_data(gdey_epd_dev_t *dev, const uint8_t *data, size_t len);

#ifdef __cplusplus
}
#endif
