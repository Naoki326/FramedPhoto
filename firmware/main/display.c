/*
 * display.c — 显示调度任务
 *
 * M2 流程：
 *   1. 挂载 storage 分区（SPIFFS）
 *   2. EPD init
 *   3. 主循环：
 *      - WiFi 未连接 → 离线演示（白屏/棋盘格，便于无网调试）
 *      - WiFi 已连接 → 拉取内容清单，新图片则下载到 /spiffs/last.fps6
 *        并从文件流式渲染（两阶段：先 IC0 全部行，再 IC1 全部行）
 *      - 按 CONFIG_FRAMEDPHOTO_POLL_INTERVAL_MIN 轮询
 *
 * 渲染为流式：行缓冲 600B，不从文件加载整帧，RAM 占用最小。
 */
#include <stdio.h>
#include <string.h>
#include "esp_check.h"
#include "esp_log.h"
#include "esp_spiffs.h"
#include "esp_sleep.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "gdeb0709e01.h"
#include "wifi_app.h"
#include "content_client.h"
#include "display.h"

static const char *TAG = "display";

#define IMAGE_PATH "/spiffs/last.fps6"
#define FPS6_HEADER_SIZE 20
#define DEMO_ONCE 1

#define NVS_NS "display"
#define NVS_KEY_LAST_ID "last_id"

static gdey_epd_dev_t *s_epd;
static char s_last_id[64] = "";
static bool s_shown_any = false;

/* ---------------- NVS 记忆上次显示内容 ---------------- */

static void nvs_load_last_id(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        size_t len = sizeof(s_last_id);
        if (nvs_get_str(h, NVS_KEY_LAST_ID, s_last_id, &len) != ESP_OK) {
            s_last_id[0] = '\0';
        }
        nvs_close(h);
    }
    ESP_LOGI(TAG, "last content: '%s'", s_last_id);
}

static void nvs_save_last_id(const char *id)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_str(h, NVS_KEY_LAST_ID, id);
        nvs_commit(h);
        nvs_close(h);
    }
}

/* ---------------- 离线演示图案（M1，无网兜底） ---------------- */

static const uint8_t s_nibbles[6] = {
    EPD_COLOR_BLACK, EPD_COLOR_WHITE, EPD_COLOR_YELLOW,
    EPD_COLOR_RED,   EPD_COLOR_BLUE,  EPD_COLOR_GREEN,
};

static uint8_t nibble_of(int idx)
{
    return s_nibbles[idx % 6];
}

static void build_pattern_row(int y, uint8_t *row)
{
    for (int x = 0; x < GDEB0709E01_WIDTH; x += 2) {
        int k = (GDEB0709E01_WIDTH - 2 - x) / 2;
        int c1 = (x / 200 + y / 200) % 6;
        int c2 = ((x + 1) / 200 + y / 200) % 6;
        row[k] = (nibble_of(c1) << 4) | nibble_of(c2);
    }
}

typedef void (*row_builder_t)(int y, uint8_t *row);

static esp_err_t show_frame_stream(row_builder_t builder)
{
    gdey_epd_dev_t *dev = s_epd;
    uint8_t row[GDEB0709E01_WIDTH / 2];
    const size_t half = dev->bytes_per_ic_row;

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

/* ---------------- 从文件渲染 FPS6（M2） ---------------- */

static esp_err_t render_fps6_file(const char *path)
{
    gdey_epd_dev_t *dev = s_epd;
    FILE *fp = fopen(path, "rb");
    if (!fp) {
        ESP_LOGE(TAG, "open %s failed", path);
        return ESP_FAIL;
    }

    uint8_t row[GDEB0709E01_WIDTH / 2]; /* 600B：IC0 前 300B + IC1 后 300B */
    esp_err_t err = ESP_OK;

    /* 阶段 1：IC0 全部行（每行前 300B） */
    fseek(fp, FPS6_HEADER_SIZE, SEEK_SET);
    if ((err = gdeb0709e01_begin(dev)) != ESP_OK) {
        goto out;
    }
    for (int y = 0; y < dev->height; y++) {
        if (fread(row, 1, dev->bytes_per_row, fp) != (size_t)dev->bytes_per_row) {
            err = ESP_ERR_INVALID_RESPONSE;
            break;
        }
        if ((err = gdeb0709e01_send_ic_row(dev, row)) != ESP_OK) {
            break;
        }
    }

    /* 阶段 2：IC1 全部行（每行后 300B —— 数据区 offset 20+300 起连续） */
    if (err == ESP_OK) {
        fseek(fp, FPS6_HEADER_SIZE + dev->bytes_per_ic_row, SEEK_SET);
        if ((err = gdeb0709e01_next_ic(dev)) == ESP_OK) {
            for (int y = 0; y < dev->height; y++) {
                if (fread(row, 1, dev->bytes_per_ic_row, fp) != (size_t)dev->bytes_per_ic_row) {
                    err = ESP_ERR_INVALID_RESPONSE;
                    break;
                }
                if ((err = gdeb0709e01_send_ic_row(dev, row)) != ESP_OK) {
                    break;
                }
            }
        }
    }

out:
    fclose(fp);
    if (err == ESP_OK) {
        ESP_RETURN_ON_ERROR(gdeb0709e01_end(dev), TAG, "end");
    }
    ESP_LOGI(TAG, "rendered %s (%s)", path, err == ESP_OK ? "ok" : esp_err_to_name(err));
    return err;
}

/* ---------------- 主流程 ---------------- */

static esp_err_t mount_storage(void)
{
    esp_vfs_spiffs_conf_t conf = {
        .base_path = "/spiffs",
        .partition_label = "storage",
        .max_files = 5,
        .format_if_mount_failed = true,
    };
    esp_err_t err = esp_vfs_spiffs_register(&conf);
    if (err == ESP_OK) {
        size_t total = 0, used = 0;
        esp_spiffs_info("storage", &total, &used);
        ESP_LOGI(TAG, "storage: %u/%u bytes used", (unsigned)used, (unsigned)total);
    }
    return err;
}

static esp_err_t update_from_server(void)
{
    char id[64];
    esp_err_t err = content_fetch_current_id(id, sizeof(id));
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "no content: %s", esp_err_to_name(err));
        return err;
    }
    if (strcmp(id, s_last_id) == 0) {
        ESP_LOGI(TAG, "content unchanged (%s)", id);
        return ESP_OK;
    }
    err = content_download_image(id, IMAGE_PATH);
    if (err != ESP_OK) {
        return err;
    }
    err = render_fps6_file(IMAGE_PATH);
    if (err == ESP_OK) {
        snprintf(s_last_id, sizeof(s_last_id), "%s", id);
        nvs_save_last_id(s_last_id);
        s_shown_any = true;
        ESP_LOGI(TAG, "now showing %s", id);
    }
    return err;
}

/* 深度休眠：RTC 定时器唤醒；唤醒后从 app_main 重启，内容未变则不再刷屏 */
static void maybe_deep_sleep(void)
{
    if (CONFIG_FRAMEDPHOTO_DEEP_SLEEP_HOURS <= 0) {
        return;
    }
    ESP_LOGI(TAG, "deep sleep %d h", CONFIG_FRAMEDPHOTO_DEEP_SLEEP_HOURS);
    esp_sleep_enable_timer_wakeup(
        (uint64_t)CONFIG_FRAMEDPHOTO_DEEP_SLEEP_HOURS * 3600ULL * 1000000ULL);
    esp_deep_sleep_start();
}

static void display_task(void *arg)
{
    s_epd = (gdey_epd_dev_t *)arg;
    ESP_LOGI(TAG, "display task started (%dx%d)", s_epd->width, s_epd->height);

    esp_err_t err = gdeb0709e01_init(s_epd);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "epd init failed: %s", esp_err_to_name(err));
        return;
    }

    nvs_load_last_id();
    err = mount_storage();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "storage mount failed, content mode disabled: %s", esp_err_to_name(err));
    }

    for (;;) {
        if (wifi_app_wait_connected(3000) == ESP_OK) {
            update_from_server();
        } else if (!s_shown_any) {
            /* 离线且从未显示过：演示棋盘格 */
            ESP_LOGI(TAG, "offline: demo checkerboard");
            if (show_frame_stream(build_pattern_row) == ESP_OK) {
                s_shown_any = true;
            }
        }
        maybe_deep_sleep();
        vTaskDelay(pdMS_TO_TICKS(CONFIG_FRAMEDPHOTO_POLL_INTERVAL_MIN * 60 * 1000));
    }
}

esp_err_t display_start(gdey_epd_dev_t *epd)
{
    /* 固定 CPU1：刷屏/等 BUSY 会长时间占核，避免饿死 CPU0 的 IDLE0 watchdog */
    BaseType_t ok = xTaskCreatePinnedToCore(display_task, "display", 8192, epd, 5, NULL, 1);
    return ok == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}
