/*
 * gdeb0709e01.h — GDEB0709E01 型号接口
 *
 * E Ink Spectra 6 彩色电子纸, 1200x1600。
 * 命令序列尚未定稿：待官方数据手册到位后实现（见 roadmap M1）。
 */
#pragma once

#include "gdey_epd.h"

#ifdef __cplusplus
extern "C" {
#endif

#define GDEB0709E01_WIDTH  1200
#define GDEB0709E01_HEIGHT 1600

/**
 * 创建 GDEB0709E01 设备描述（引脚取 Kconfig 默认值）。
 * 返回前会做总线初始化 + 硬件复位。
 */
esp_err_t gdeb0709e01_create(gdey_epd_dev_t *dev);

/**
 * 完整初始化（复位 + 初始化命令序列 + 清屏）。
 * TODO(M1): 填入数据手册的 init sequence / 清屏序列后实现。
 */
esp_err_t gdeb0709e01_init(gdey_epd_dev_t *dev);

#ifdef __cplusplus
}
#endif
