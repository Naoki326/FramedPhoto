/*
 * wake_align.c — 唤醒对齐
 *
 * 设备休眠唤醒节拍对齐到本地 HH:01 / HH:31（每个整点 +1 分钟 / +31 分钟），
 * 刷新时刻可预期。工作方式：
 *   1. 心跳是设备与服务器每次连接的必经点，响应 server_time（UTC epoch 秒）
 *      由 device_client 写入系统时钟 —— 本地服务端就在局域网（mDNS .local），
 *      外网 NTP（SNTP）不保证可达，由心跳带时间最直接；
 *   2. 深睡前按系统时钟算出距下一个 :01/:31 边界的时长，交给 RTC 定时器；
 *   3. ESP32 深睡期间 RTC 持续计时、系统时间跨深睡保留，因此偶发心跳失败
 *      （服务端临时离线）仍按上次校准的时间对齐；只有时钟从未校准过
 *      （全新上电且一直连不上服务端）时回退固定间隔（display 负责）。
 *
 * 边界计算是纯整数算术：时区固定偏移（FRAMEDPHOTO_UTC_OFFSET_HOURS，
 * 北京时间 = 8），不经过 newlib 的 TZ/localtime/mktime。0.1.10 真机教训：
 * libc 时区路径在目标机上静默给出坏值（每轮回退固定间隔且无从定位），
 * 固定偏移场景根本不需要 libc —— 算术实现宿主机与目标机行为完全一致。
 */
#include <time.h>
#include "esp_log.h"
#include "sdkconfig.h"
#include "wake_align.h"

static const char *TAG = "wake_align";

/* 距边界不足此时长则顺延到下一个边界：唤醒后还要走完开机/连网/拉取全流程，
 * 贴边唤醒（<1 min）只会多耗一次启动电，顺延半点更划算。 */
#define WAKE_ALIGN_MIN_LEAD_S 60

/* 边界定义：每个整点的第 1 分钟与第 31 分钟 */
#define WAKE_ALIGN_MINUTE 1
#define WAKE_ALIGN_PERIOD_MIN 30

time_t wake_align_next_boundary(time_t now)
{
    /* 本地钟面 = UTC + 固定偏移。偏移为整小时，:01/:31 边界的 epoch
     * 只与「本地分钟」有关，因此先换算到本地日的秒轴上做纯整数运算 */
    int64_t local_now = (int64_t)now + (int64_t)CONFIG_FRAMEDPHOTO_UTC_OFFSET_HOURS * 3600;
    int64_t secs_today = ((local_now % 86400) + 86400) % 86400; /* 本地 0 点起秒数 */
    int64_t midnight = local_now - secs_today;                  /* 本地 0 点的 epoch */
    int min = (int)(secs_today / 60 % 60);
    int hour = (int)(secs_today / 3600 % 24);

    int64_t target_local; /* 目标时刻的「本地 0 点起秒数」 */
    if (min < WAKE_ALIGN_MINUTE) {
        target_local = (int64_t)hour * 3600 + WAKE_ALIGN_MINUTE * 60;         /* :00 → :01 */
    } else if (min < WAKE_ALIGN_MINUTE + WAKE_ALIGN_PERIOD_MIN) {
        target_local = (int64_t)hour * 3600 +
                       (WAKE_ALIGN_MINUTE + WAKE_ALIGN_PERIOD_MIN) * 60;      /* :01-:30 → :31 */
    } else {
        target_local = (int64_t)(hour + 1) * 3600 + WAKE_ALIGN_MINUTE * 60;   /* :31-:59 → 次小时 :01 */
    }
    /* hour=23 时 target_local 越过 86400，epoch 加法天然落到次日 0 点后 */

    /* midnight 是「本地 0 点在 UTC 轴上的数值」（已含偏移），
     * 加上本地秒数后须减回偏移才是 epoch */
    int64_t target = midnight + target_local -
                     (int64_t)CONFIG_FRAMEDPHOTO_UTC_OFFSET_HOURS * 3600;
    if ((double)(target - now) < WAKE_ALIGN_MIN_LEAD_S) {
        target += (int64_t)WAKE_ALIGN_PERIOD_MIN * 60;
    }
    return (time_t)target;
}

int64_t wake_align_next_wake_us_at(time_t now)
{
#if !CONFIG_FRAMEDPHOTO_ALIGN_WAKE
    ESP_LOGW(TAG, "alignment disabled in menuconfig");
    return -1;
#else
    /* 墙钟从未校准：epoch 远小于任何真实日期（1e9 ≈ 2001-09） */
    if (now < 1000000000) {
        ESP_LOGW(TAG, "wall clock not calibrated (no heartbeat yet), alignment unavailable");
        return -1;
    }

    time_t target = wake_align_next_boundary(now);
    if (target <= now) {
        ESP_LOGW(TAG, "boundary calc failed (target=%lld now=%lld), alignment unavailable",
                 (long long)target, (long long)now);
        return -1;
    }

    int64_t secs = (int64_t)(target - now);
    int64_t us = secs * 1000000;
    /* 钟面显示用算术换算（本地时 = epoch + 偏移），同样不依赖 libc 时区 */
    time_t local_target = target + (time_t)CONFIG_FRAMEDPHOTO_UTC_OFFSET_HOURS * 3600;
    struct tm gt;
    gmtime_r(&local_target, &gt);
    ESP_LOGI(TAG, "next aligned wake in %lld s, at local %02d:%02d:%02d (UTC%+d)",
             (long long)secs, gt.tm_hour, gt.tm_min, gt.tm_sec,
             CONFIG_FRAMEDPHOTO_UTC_OFFSET_HOURS);
    return us;
#endif
}
