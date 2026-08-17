/*
 * host 测试桩：esp_err.h 的最小替代面（仅 http_util 编译所需）。
 * 常量取值与 ESP-IDF esp_err.h 保持一致，保证 host 断言的错误码语义与目标机相同。
 */
#ifndef HTTP_TESTS_SHIM_ESP_ERR_H
#define HTTP_TESTS_SHIM_ESP_ERR_H

typedef int esp_err_t;

#define ESP_OK  0
#define ESP_FAIL -1

#define ESP_ERR_NO_MEM          0x101
#define ESP_ERR_INVALID_ARG     0x102
#define ESP_ERR_INVALID_STATE   0x103
#define ESP_ERR_INVALID_SIZE    0x104
#define ESP_ERR_NOT_FOUND       0x105
#define ESP_ERR_NOT_SUPPORTED   0x106
#define ESP_ERR_TIMEOUT         0x107
#define ESP_ERR_INVALID_RESPONSE 0x108

static inline const char *esp_err_to_name_shim(esp_err_t e)
{
    switch (e) {
    case ESP_OK: return "ESP_OK";
    case ESP_FAIL: return "ESP_FAIL";
    case ESP_ERR_NO_MEM: return "ESP_ERR_NO_MEM";
    case ESP_ERR_INVALID_ARG: return "ESP_ERR_INVALID_ARG";
    case ESP_ERR_INVALID_STATE: return "ESP_ERR_INVALID_STATE";
    case ESP_ERR_INVALID_SIZE: return "ESP_ERR_INVALID_SIZE";
    case ESP_ERR_NOT_FOUND: return "ESP_ERR_NOT_FOUND";
    case ESP_ERR_NOT_SUPPORTED: return "ESP_ERR_NOT_SUPPORTED";
    case ESP_ERR_TIMEOUT: return "ESP_ERR_TIMEOUT";
    case ESP_ERR_INVALID_RESPONSE: return "ESP_ERR_INVALID_RESPONSE";
    default: return "UNKNOWN";
    }
}
#define esp_err_to_name(e) esp_err_to_name_shim(e)

#endif /* HTTP_TESTS_SHIM_ESP_ERR_H */
