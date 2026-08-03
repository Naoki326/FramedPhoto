/*
 * display.c — 显示调度任务
 *
 * 当前为 M1 演示模式（无需真机即可编译验证）：
 *   1. init 序列
 *   2. 全屏纯色 / 6 色横条 / 棋盘格 循环演示
 *
 * 图案全部流式生成（600B 行缓冲），不占整帧 RAM。
 * TODO(M2): 接入 HTTP 内容拉取，替换演示循环。
 */
#include <string.h>
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "gdeb0709e01.h"
#include "display.h"

static const char *TAG = "display";

#define DEMO_INTERVAL_MS 8000

/* 6 色 nibble 表（索引 0..5 -> 设备 nibble） */
static const uint8_t s_nibbles[6] = {
    EPD_COLOR_BLACK, EPD_COLOR_WHITE, EPD_COLOR_YELLOW,
    EPD_COLOR_RED,   EPD_COLOR_BLUE,  EPD_COLOR_GREEN,
};

static uint8_t nibble_of(int idx)
{
    return s_nibbles[idx % 6];
}

/*
 * 行缓冲布局（与 FPS6 设备格式一致）：
 *   k = (1198 - x) / 2  →  逻辑像素 x 落在字节 k
 *   k < 300  → IC0（右半屏）；k >= 300 → IC1（左半屏）
 *   高 nibble = 偶数像素 x，低 nibble = x+1
 */
static void build_pattern_row(int y, uint8_t *row)
{
    for (int x = 0; x < GDEB0709E01_WIDTH; x += 2) {
        int k = (GDEB0709E01_WIDTH - 2 - x) / 2;
        int c1 = (x / 200 + y / 200) % 6;
        int c2 = ((x + 1) / 200 + y / 200) % 6;
        row[k] = (nibble_of(c1) << 4) | nibble_of(c2);
    }
}

/* 6 色横向色条（y 方向 6 段，段内整行同色） */
static void build_colorbar_row(int y, uint8_t *row)
{
    int seg = y / (GDEB0709E01_HEIGHT / 6);
    uint8_t b = EPD_COLOR_BYTE(nibble_of(seg));
    memset(row, b, GDEB0709E01_WIDTH / 2);
}

typedef void (*row_builder_t)(int y, uint8_t *row);

/* 两阶段流式送帧：IC0 全部行 → IC1 全部行 → 刷新 */
static esp_err_t show_frame_stream(gdey_epd_dev_t *dev, row_builder_t builder)
{
    uint8_t row[GDEB0709E01_WIDTH / 2]; /* 600B */
    const size_t half = dev->bytes_per_ic_row; /* 300 */

    ESP_RETURN_ON_ERROR(gdeb0709e01_begin(dev), TAG, "begin");
    for (int y = 0; y < dev->height; y++) {
        builder(y, row);
        ESP_RETURN_ON_ERROR(gdeb0709e01_send_ic_row(dev, row), TAG, "ic0 y=%d", y);
    }
    ESP_RETURN_ON_ERROR(gdeb0709e01_next_ic(dev), TAG, "next_ic");
    for (int y = 0; y < dev->height; y++) {
        builder(y, row);
        ESP_RETURN_ON_ERROR(gdeb0709e01_send_ic_row(dev, row + half), TAG, "ic1 y=%d", y);
    }
    return gdeb0709e01_end(dev);
}

static void display_task(void *arg)
{
    gdey_epd_dev_t *epd = (gdey_epd_dev_t *)arg;
    ESP_LOGI(TAG, "display task started (%dx%d)", epd->width, epd->height);

    esp_err_t err = gdeb0709e01_init(epd);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "epd init failed: %s", esp_err_to_name(err));
        return;
    }
    ESP_LOGI(TAG, "M1 demo loop start");

    for (;;) {
        /* 1. 白色全屏 */
        ESP_LOGI(TAG, "demo: fill white");
        gdeb0709e01_fill(epd, EPD_COLOR_BYTE(EPD_COLOR_WHITE));
        vTaskDelay(pdMS_TO_TICKS(DEMO_INTERVAL_MS));

        /* 2. 6 色横条 */
        ESP_LOGI(TAG, "demo: color bar");
        show_frame_stream(epd, build_colorbar_row);
        vTaskDelay(pdMS_TO_TICKS(DEMO_INTERVAL_MS));

        /* 3. 棋盘格 */
        ESP_LOGI(TAG, "demo: checkerboard");
        show_frame_stream(epd, build_pattern_row);
        vTaskDelay(pdMS_TO_TICKS(DEMO_INTERVAL_MS));
    }
}

esp_err_t display_start(gdey_epd_dev_t *epd)
{
    BaseType_t ok = xTaskCreate(display_task, "display", 8192, epd, 5, NULL);
    return ok == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}
