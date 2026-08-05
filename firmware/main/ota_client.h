/*
 * ota_client.h — 设备端 OTA 客户端
 */
#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * 启动时调用（尽可能早）：若本固件是 OTA 后首次运行（PENDING_VERIFY），
 * 标记有效并取消回滚。崩溃场景下 bootloader 会自动切回旧分区。
 */
void ota_client_init(void);

/**
 * 检查服务端是否有新固件；有则流式下载（esp_ota_write + sha256 校验）、
 * 设置启动分区并重启。
 *
 * @return ESP_OK        已升级并重启（本函数不返回）；
 *         ESP_ERR_NOT_FOUND 无新固件（正常，无需升级）；
 *         其他错误码       检查/下载失败，保持当前固件（下次轮询重试）。
 */
esp_err_t ota_client_maybe_update(void);

#ifdef __cplusplus
}
#endif
