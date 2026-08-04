/*
 * wifi_app.h — WiFi STA 连接管理
 *
 * 凭据来源优先级：NVS ("wifi" namespace) > menuconfig 默认值。
 * 凭据可通过 wifi_config（SoftAP 配网）或服务端推送写入 NVS，无需重新编译。
 */
#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 初始化 WiFi 栈并启动 STA 连接；无有效凭据时返回 ESP_ERR_INVALID_STATE */
esp_err_t wifi_app_start(void);

/** 阻塞等待 WiFi 已连接，timeout_ms 超时返回 ESP_ERR_TIMEOUT */
esp_err_t wifi_app_wait_connected(uint32_t timeout_ms);

/** NVS / menuconfig 是否存在有效凭据 */
bool wifi_app_has_credentials(void);

/** 写入凭据到 NVS（运行时配网/服务端推送用），写成功后设备重启即可生效 */
esp_err_t wifi_app_save_credentials(const char *ssid, const char *pass);

#ifdef __cplusplus
}
#endif
