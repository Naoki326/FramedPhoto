/*
 * device_client.h — 设备注册 / 心跳 / 服务端指令拉取
 *
 * 每次 WiFi 连接后调用 device_client_heartbeat()：
 *   - 上报固件版本 / 当前内容 / IP / 运行时长，自动注册设备
 *   - 响应若携带待下发 WiFi 配置（服务端管理台设置），写入 NVS 并重启切换网络
 */
#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 上报心跳并处理服务端下发的 WiFi 配置（需已连接 WiFi）。失败返回非零。 */
esp_err_t device_client_heartbeat(void);

#ifdef __cplusplus
}
#endif
