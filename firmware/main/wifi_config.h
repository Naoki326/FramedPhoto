/*
 * wifi_config.h — 运行时配网（SmartConfig 主 + SoftAP 兜底）
 *
 * 设备无有效 WiFi 凭据时：
 *   - wifi_config_start_smartconfig()：ESPTouch v1 监听（约 5 分钟），
 *     服务端/App 在局域网广播配置，全程无人断网；
 *   - 监听超时后自动开 SoftAP（FramedPhoto-XXXX，192.168.4.1）手机配网页兜底。
 */
#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 启动 SmartConfig 监听（独立任务，超时后自动切 SoftAP） */
esp_err_t wifi_config_start_smartconfig(void);

/** 启动 SoftAP + 配网 HTTP 服务（需已初始化 WiFi 栈） */
esp_err_t wifi_config_start_ap(void);

#ifdef __cplusplus
}
#endif
