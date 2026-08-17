/*
 * device_id.h — 设备标识（全固件唯一一份）
 *
 * MAC 派生、幂等：同机重启稳定。心跳注册（device_client）与 OTA
 * manifest（ota_client）共用同一格式，避免两份拷贝各自漂移。
 */
#ifndef DEVICE_ID_H
#define DEVICE_ID_H

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include "esp_mac.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 派生自 WiFi STA MAC 后三字节，形如 "esp32s3-xxxxxx" */
static inline void device_id(char *out, size_t len)
{
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(out, len, "esp32s3-%02x%02x%02x", mac[3], mac[4], mac[5]);
}

#ifdef __cplusplus
}
#endif

#endif /* DEVICE_ID_H */
