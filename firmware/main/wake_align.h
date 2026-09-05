/*
 * wake_align.h — 唤醒对齐：深睡唤醒时刻对齐到本地半点边界 +1 分钟
 *
 * 目标：设备每次唤醒的墙钟时刻落在本地 HH:01 / HH:31，刷新节拍可预期。
 * 时间来源是心跳响应的 server_time（见 device_client / wake_align.c 注释）。
 */
#pragma once
#include <stdint.h>
#include <time.h>

/* 纯函数：now（epoch 秒）之后下一个本地 :01/:31 边界的 epoch 秒。
 * 纯整数算术（时区偏移来自 menuconfig FRAMEDPHOTO_UTC_OFFSET_HOURS），
 * 不依赖 newlib 的 TZ/localtime/mktime（真机 0.1.10 教训：该路径
 * 在目标机上返回异常结果且难观测，固定偏移场景无需 libc）。
 * 距边界不足一分钟时顺延到下一个边界。 */
time_t wake_align_next_boundary(time_t now);

/* 距下一个 :01/:31 边界的微秒数；未启用对齐或 now 不是可信墙钟
 * （< 2001 年，即从未心跳校准）时返回 -1（原因写入日志），
 * 调用方应回退固定间隔。now 由 device_client_wall_time 提供。 */
int64_t wake_align_next_wake_us_at(time_t now);
