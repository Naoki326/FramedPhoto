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
#include "esp_partition.h"
#include "esp_sleep.h"
#include "driver/gpio.h"
#include "device_client.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "gdeb0709e01.h"
#include "wifi_app.h"
#include "content_client.h"
#include "ota_client.h"
#include "display.h"

static const char *TAG = "display";

#define FPS6_HEADER_SIZE 20
#define FPS6_IMAGE_SIZE  (960000 + FPS6_HEADER_SIZE)   /* 1200x1600 4bit + 头 */
#define STORAGE_ERASE_SIZE 0x100000                     /* 1MB，4KB 对齐，覆盖整图 */
#define DEMO_ONCE 1

#define NVS_NS "display"
#define NVS_KEY_LAST_ID "last_id"

static gdey_epd_dev_t *s_epd;
static char s_last_id[64] = "";
static bool s_shown_any = false;
static const esp_partition_t *s_storage;  /* storage 裸分区：存最新 FPS6 图 */

/* ---------------- 刷新按钮（M4+：上传后按一下立即刷新） ---------------- */

#define BTN_GPIO_NUM ((gpio_num_t)CONFIG_FRAMEDPHOTO_BUTTON_GPIO)
#define BTN_ENABLED  (CONFIG_FRAMEDPHOTO_BUTTON_GPIO >= 0)

static SemaphoreHandle_t s_btn_sem; /* 按钮 ISR → 主循环唤醒 */

static void IRAM_ATTR btn_isr(void *arg)
{
    BaseType_t hp = pdFALSE;
    if (s_btn_sem) {
        xSemaphoreGiveFromISR(s_btn_sem, &hp);
    }
    if (hp) {
        portYIELD_FROM_ISR(hp);
    }
}

static void button_init(void)
{
    if (!BTN_ENABLED) {
        ESP_LOGI(TAG, "refresh button disabled");
        return;
    }
    s_btn_sem = xSemaphoreCreateBinary();
    if (!s_btn_sem) {
        ESP_LOGW(TAG, "btn sem alloc failed");
        return;
    }
    gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << CONFIG_FRAMEDPHOTO_BUTTON_GPIO,
        .mode = GPIO_MODE_INPUT,
#if CONFIG_FRAMEDPHOTO_BUTTON_ACTIVE_LOW
        .pull_up_en = GPIO_PULLUP_ENABLE,   /* 按下接地 = 低，默认高 */
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
#else
        .pull_up_en = GPIO_PULLUP_DISABLE,  /* 按下接 VCC = 高，默认低 */
        .pull_down_en = GPIO_PULLDOWN_ENABLE,
#endif
        .intr_type = CONFIG_FRAMEDPHOTO_BUTTON_ACTIVE_LOW ? GPIO_INTR_NEGEDGE
                                                          : GPIO_INTR_POSEDGE,
    };
    if (gpio_config(&cfg) != ESP_OK ||
        gpio_install_isr_service(0) != ESP_OK ||
        gpio_isr_handler_add(BTN_GPIO_NUM, btn_isr, NULL) != ESP_OK) {
        ESP_LOGW(TAG, "refresh button init failed");
        vSemaphoreDelete(s_btn_sem);
        s_btn_sem = NULL;
        return;
    }
    ESP_LOGI(TAG, "refresh button on GPIO%d (press = refresh now)",
             CONFIG_FRAMEDPHOTO_BUTTON_GPIO);
}

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
    /* LTR 布局：字节 k 对应像素对 (2k, 2k+1)，与 FPS6 数据契约一致 */
    for (int x = 0; x < GDEB0709E01_WIDTH; x += 2) {
        int k = x / 2;
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

/* ---------------- 从 storage 裸分区渲染 FPS6 ---------------- */

static esp_err_t render_fps6_partition(void)
{
    gdey_epd_dev_t *dev = s_epd;
    uint8_t row[GDEB0709E01_WIDTH / 2]; /* 600B：IC0 前 300B（左半）+ IC1 后 300B（右半） */
    esp_err_t err = ESP_OK;

    /* 阶段 1：IC0 全部行（每行前 300B，左半屏） */
    if ((err = gdeb0709e01_begin(dev)) != ESP_OK) {
        goto out;
    }
    for (int y = 0; y < dev->height; y++) {
        if (esp_partition_read(s_storage, FPS6_HEADER_SIZE + (long)y * dev->bytes_per_row,
                               row, dev->bytes_per_row) != ESP_OK) {
            err = ESP_ERR_INVALID_RESPONSE;
            break;
        }
        if ((err = gdeb0709e01_send_ic_row(dev, row)) != ESP_OK) {
            break;
        }
    }

    /* 阶段 2：IC1 全部行（每行后 300B，右半屏）。
     * FPS6 数据区为「逐行 600B 交织」布局（行 y: offset 20+y*600，前 300B 送 IC0、
     * 后 300B 送 IC1），与官方 pic_display_test 的 num+i*Width / num+i*Width+Width1 一致。
     * 切勿把 IC0/IC1 数据当作两个连续大块线性读取——那样从第 2 行起就会错位。 */
    if (err == ESP_OK) {
        if ((err = gdeb0709e01_next_ic(dev)) == ESP_OK) {
            for (int y = 0; y < dev->height; y++) {
                if (esp_partition_read(s_storage,
                                       FPS6_HEADER_SIZE + (long)y * dev->bytes_per_row
                                           + dev->bytes_per_ic_row,
                                       row, dev->bytes_per_ic_row) != ESP_OK) {
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
    if (err == ESP_OK) {
        ESP_RETURN_ON_ERROR(gdeb0709e01_end(dev), TAG, "end");
    }
    ESP_LOGI(TAG, "rendered partition (%s)", err == ESP_OK ? "ok" : esp_err_to_name(err));
    return err;
}

/* ---------------- 主流程 ---------------- */

static esp_err_t mount_storage(void)
{
    /* storage 分区不挂文件系统，直接 esp_partition 裸读写：
     * 写 flash 比 SPIFFS 文件系统快一个量级（下载 1MB 由 ~100s 降到秒级），
     * 渲染读取同样受益。SPIFFS 顺序写无预分配是此前下载慢的根因。 */
    s_storage = esp_partition_find_first(ESP_PARTITION_TYPE_DATA,
                                         ESP_PARTITION_SUBTYPE_ANY, "storage");
    if (!s_storage) {
        ESP_LOGE(TAG, "storage partition not found");
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "storage partition: %u KB @0x%lx", s_storage->size / 1024,
             (unsigned long)s_storage->address);
    return ESP_OK;
}

static esp_err_t update_from_server(bool force)
{
    /* 心跳上报 + 处理服务端下发的 WiFi 配置（失败不阻塞内容流程） */
    device_client_heartbeat();

    /* OTA：有新固件则升级重启；无新固件/失败不打扰显示流程 */
    esp_err_t ota_err = ota_client_maybe_update();
    if (ota_err != ESP_OK && ota_err != ESP_ERR_NOT_FOUND) {
        ESP_LOGW(TAG, "ota check failed: %s", esp_err_to_name(ota_err));
    }

    char id[64];
    char url[256];
    esp_err_t err = content_fetch_current(id, sizeof(id), url, sizeof(url));
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "no content: %s", esp_err_to_name(err));
        return err;
    }
    /* force（按钮按下/按钮唤醒）：跳过 unchanged 短路，强制重新下载渲染 */
    if (!force && strcmp(id, s_last_id) == 0) {
        ESP_LOGI(TAG, "content unchanged (%s)", id);
        return ESP_OK;
    }
    /* 下载前擦除 storage 分区（裸写 flash 要求目标已擦除） */
    esp_err_t e = esp_partition_erase_range(s_storage, 0, STORAGE_ERASE_SIZE);
    if (e != ESP_OK) {
        ESP_LOGE(TAG, "erase storage failed: %s", esp_err_to_name(e));
        return e;
    }
    err = content_download_image(url, s_storage);
    if (err != ESP_OK) {
        return err;
    }
    err = render_fps6_partition();
    if (err == ESP_OK) {
        snprintf(s_last_id, sizeof(s_last_id), "%s", id);
        nvs_save_last_id(s_last_id);
        s_shown_any = true;
        ESP_LOGI(TAG, "now showing %s", id);
    }
    return err;
}

/* 深度休眠：RTC 定时器 + 按钮（EXT1）唤醒；唤醒后从 app_main 重启，
 * 内容未变则不再刷屏（按钮唤醒除外——用户主动按 = 想看新内容，强制刷）。 */
static void maybe_deep_sleep(void)
{
    if (CONFIG_FRAMEDPHOTO_DEEP_SLEEP_HOURS <= 0) {
        return;
    }
    if (BTN_ENABLED) {
        esp_sleep_ext1_wakeup_mode_t mode = CONFIG_FRAMEDPHOTO_BUTTON_ACTIVE_LOW
                                                ? ESP_EXT1_WAKEUP_ANY_LOW
                                                : ESP_EXT1_WAKEUP_ANY_HIGH;
        ESP_ERROR_CHECK(esp_sleep_enable_ext1_wakeup_io(
            1ULL << CONFIG_FRAMEDPHOTO_BUTTON_GPIO, mode));
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
    /* 重启后若 NVS 有显示记录（说明上次成功显示过内容），视为已显示过：
     * 无网时保持屏幕画面不变（E Ink 双稳态，不刷屏即保持），不覆盖为棋盘格。 */
    s_shown_any = (s_last_id[0] != '\0');
    err = mount_storage();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "storage mount failed, content mode disabled: %s", esp_err_to_name(err));
    }

    button_init();
    /* 唤醒原因决定首次是否强制刷新：
     * - EXT1（按钮唤醒）/ POWERON（上电复位）→ 强制刷一次，
     *   确认设备工作、立即可见最新内容（用户明确要上电刷屏）
     * - TIMER（RTC 深睡唤醒）→ 不强制，内容未变不刷屏（省电） */
    esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();
    bool first_force = (cause == ESP_SLEEP_WAKEUP_EXT1) ||
                       (cause == ESP_SLEEP_WAKEUP_UNDEFINED);
    if (cause == ESP_SLEEP_WAKEUP_EXT1) {
        ESP_LOGI(TAG, "woken by refresh button");
    } else if (cause == ESP_SLEEP_WAKEUP_UNDEFINED) {
        ESP_LOGI(TAG, "power-on boot: forcing first refresh");
    }

    bool btn_pressed = false;
    for (;;) {
        /* 上一轮等待期间按钮被按下（或深度休眠 EXT1 唤醒）→ 本轮强制刷新 */
        bool force = first_force || btn_pressed;
        first_force = false;
        btn_pressed = false;
        if (force) {
            ESP_LOGI(TAG, "refresh button: forcing update");
        }

        /* 先执行一次内容拉取 + OTA 检查，再进入轮询等待 */
        if (wifi_app_wait_connected(force ? 15000 : 3000) == ESP_OK) {
            update_from_server(force);
        } else if (!s_shown_any) {
            /* 离线兜底：仅全新设备（NVS 无记录）显示棋盘格便于无网调试；
             * 已有内容的设备无网时保持屏幕画面不变。 */
            ESP_LOGI(TAG, "offline: demo checkerboard (first boot)");
            if (show_frame_stream(build_pattern_row) == ESP_OK) {
                s_shown_any = true;
            }
        }
        maybe_deep_sleep();

        /* 轮询等待：按钮按下立即返回；否则等到下一轮 */
        if (s_btn_sem) {
            btn_pressed = (xSemaphoreTake(s_btn_sem,
                            pdMS_TO_TICKS(CONFIG_FRAMEDPHOTO_POLL_INTERVAL_MIN * 60 * 1000)) == pdTRUE);
        } else {
            vTaskDelay(pdMS_TO_TICKS(CONFIG_FRAMEDPHOTO_POLL_INTERVAL_MIN * 60 * 1000));
        }
    }
}

esp_err_t display_start(gdey_epd_dev_t *epd)
{
    /* 固定 CPU1：刷屏/等 BUSY 会长时间占核，避免饿死 CPU0 的 IDLE0 watchdog */
    BaseType_t ok = xTaskCreatePinnedToCore(display_task, "display", 8192, epd, 5, NULL, 1);
    return ok == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}
