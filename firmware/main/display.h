/*
 * display.h — 显示调度（内容拉取 + 渲染 + 刷新）
 */
#pragma once

#include "esp_err.h"
#include "gdey_epd.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * 创建显示任务：
 *   - M1: 驱动层就绪后输出测试图案（占位）
 *   - M2+: 轮询服务端内容清单，下载图片，渲染并刷新
 */
esp_err_t display_start(gdey_epd_dev_t *epd);

#ifdef __cplusplus
}
#endif
