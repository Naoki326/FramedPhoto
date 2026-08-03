/*
 * wifi_app.h — WiFi STA 连接管理
 */
#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 连接成功后置位（事件组），wifi_app_wait_connected() 可等 */
esp_err_t wifi_app_start(void);

/** 阻塞等待 WiFi 已连接，timeout_ms 超时返回 ESP_ERR_TIMEOUT */
esp_err_t wifi_app_wait_connected(uint32_t timeout_ms);

#ifdef __cplusplus
}
#endif
