/*
 * gdeb0709e01.h — GDEB0709E01 型号接口
 *
 * E Ink Spectra 6 彩色电子纸, 1200x1600, 双 GDEP133C02 驱动 IC。
 * 命令序列移植自官方 ESP32 示例（docs/vendor/gdeb0709e01/esp32_example/）。
 */
#pragma once

#include "gdey_epd.h"

#ifdef __cplusplus
extern "C" {
#endif

#define GDEB0709E01_WIDTH  1200
#define GDEB0709E01_HEIGHT 1600

/** 整帧数据大小（字节）：1200*1600/2 */
#define GDEB0709E01_FRAME_SIZE (GDEB0709E01_WIDTH * GDEB0709E01_HEIGHT / 2)

/** 每 IC 数据大小（字节）：600*1600/2 */
#define GDEB0709E01_IC_FRAME_SIZE (GDEB0709E01_FRAME_SIZE / 2)

/** 创建设备描述（引脚取 Kconfig 默认值），含总线初始化 + 硬件复位 */
esp_err_t gdeb0709e01_create(gdey_epd_dev_t *dev);

/** 完整初始化：复位 + init 序列（AN_TM/CMD66/PSR/.../TFT_VCOM_POWER） */
esp_err_t gdeb0709e01_init(gdey_epd_dev_t *dev);

/** 刷新：PON -> DRF -> POF（BUSY 同步） */
esp_err_t gdeb0709e01_refresh(gdey_epd_dev_t *dev);

/** 全屏纯色（流式填充，无整帧缓冲；color_byte 如 0x11=白） */
esp_err_t gdeb0709e01_fill(gdey_epd_dev_t *dev, uint8_t color_byte);

/**
 * 全屏刷图（整帧，布局与 FPS6 格式一致）：
 *   先 IC0 逐行 300B（右半屏），再 IC1 逐行 300B（左半屏），最后刷新。
 * 帧数据由调用方持有（flash const / PSRAM / 流式读取），不复制进 RAM。
 */
esp_err_t gdeb0709e01_display_image(gdey_epd_dev_t *dev, const uint8_t *frame, size_t len);

/* ---------- 流式接口（逐行发送，RAM 占用最小，供 HTTP 流 / 图案生成用） ---------- */

/** 开始一帧：选中 IC0 并开启 DTM（先送 IC0 右半屏全部行） */
esp_err_t gdeb0709e01_begin(gdey_epd_dev_t *dev);

/** 向当前选中 IC 发送一行（bytes_per_ic_row = 300B）；调用方负责每行组织 */
esp_err_t gdeb0709e01_send_ic_row(gdey_epd_dev_t *dev, const uint8_t *half_row);

/** 切换至下一个 IC（CS0 关、CS1 开并开启 DTM）；IC0 行发完后再调用 */
esp_err_t gdeb0709e01_next_ic(gdey_epd_dev_t *dev);

/** 结束一帧：释放片选并触发刷新 */
esp_err_t gdeb0709e01_end(gdey_epd_dev_t *dev);

#ifdef __cplusplus
}
#endif
