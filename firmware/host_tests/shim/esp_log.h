/*
 * host 测试桩：esp_log.h 的最小替代面（打桩为 fprintf，随 ASan 泄漏检测可见）。
 */
#ifndef HTTP_TESTS_SHIM_ESP_LOG_H
#define HTTP_TESTS_SHIM_ESP_LOG_H

#include <stdio.h>

#define ESP_LOGE(tag, fmt, ...) fprintf(stderr, "E %s: " fmt "\n", tag, ##__VA_ARGS__)
#define ESP_LOGW(tag, fmt, ...) fprintf(stderr, "W %s: " fmt "\n", tag, ##__VA_ARGS__)
#define ESP_LOGI(tag, fmt, ...) fprintf(stderr, "I %s: " fmt "\n", tag, ##__VA_ARGS__)

#endif /* HTTP_TESTS_SHIM_ESP_LOG_H */
