/*
 * wake_align 宿主机测试：下一个 :01/:31 边界的计算是纯 libc 逻辑
 * （localtime/mktime + TZ 环境），无需 ESP 运行时即可编译运行。
 *
 * 全部用例在 TZ=CST-8（北京时间）下：输入与期望都用 mktime 以本地
 * 时刻构造，断言 epoch 相等，避免手工换算 UTC 偏移。
 *
 *   make test   （见 Makefile）
 */
#include <stdlib.h>
#include <time.h>
#include "minitest.h"
#include "wake_align.h"

/* 本地时刻 → epoch（依赖已设置的 TZ） */
static time_t local(int y, int mo, int d, int h, int mi, int s)
{
    struct tm t = { .tm_year = y - 1900, .tm_mon = mo - 1, .tm_mday = d,
                    .tm_hour = h, .tm_min = mi, .tm_sec = s, .tm_isdst = -1 };
    return mktime(&t);
}

static void test_hour_start_goes_to_xx01(void)
{
    /* 09:45 → 10:01 */
    MT_ASSERT_INT_EQ(wake_align_next_boundary(local(2026, 2, 8, 9, 45, 0)),
                     local(2026, 2, 8, 10, 1, 0));
    /* 10:00:30 距 10:01 不足 1 分钟 → 顺延 10:31 */
    MT_ASSERT_INT_EQ(wake_align_next_boundary(local(2026, 2, 8, 10, 0, 30)),
                     local(2026, 2, 8, 10, 31, 0));
}

static void test_first_half_goes_to_xx31(void)
{
    /* 10:01:30 → 10:31 */
    MT_ASSERT_INT_EQ(wake_align_next_boundary(local(2026, 2, 8, 10, 1, 30)),
                     local(2026, 2, 8, 10, 31, 0));
    /* 12:15:10 → 12:31 */
    MT_ASSERT_INT_EQ(wake_align_next_boundary(local(2026, 2, 8, 12, 15, 10)),
                     local(2026, 2, 8, 12, 31, 0));
    /* 10:30:00 恰好距 10:31 整 60s（= 最小提前量）→ 保持 10:31 */
    MT_ASSERT_INT_EQ(wake_align_next_boundary(local(2026, 2, 8, 10, 30, 0)),
                     local(2026, 2, 8, 10, 31, 0));
}

static void test_second_half_goes_to_next_hour_xx01(void)
{
    /* 10:31:00 → 11:01 */
    MT_ASSERT_INT_EQ(wake_align_next_boundary(local(2026, 2, 8, 10, 31, 0)),
                     local(2026, 2, 8, 11, 1, 0));
    /* 10:59:59 → 11:01 */
    MT_ASSERT_INT_EQ(wake_align_next_boundary(local(2026, 2, 8, 10, 59, 59)),
                     local(2026, 2, 8, 11, 1, 0));
}

static void test_midnight_rollover(void)
{
    /* 23:45 → 次日 00:01（mktime 归一化 tm_hour=24） */
    MT_ASSERT_INT_EQ(wake_align_next_boundary(local(2026, 2, 8, 23, 45, 0)),
                     local(2026, 2, 9, 0, 1, 0));
    /* 23:59:30 距 00:01 有 90s → 保持 00:01 */
    MT_ASSERT_INT_EQ(wake_align_next_boundary(local(2026, 2, 8, 23, 59, 30)),
                     local(2026, 2, 9, 0, 1, 0));
    /* 23:59:59 距 00:01 有 61s（≥ 最小提前量）→ 保持 00:01 */
    MT_ASSERT_INT_EQ(wake_align_next_boundary(local(2026, 2, 8, 23, 59, 59)),
                     local(2026, 2, 9, 0, 1, 0));
}

static void test_too_close_to_boundary_slides_to_next(void)
{
    /* 00:00:59 距 00:01 仅 1s → 顺延到 00:31（贴边唤醒只多耗一次启动电） */
    MT_ASSERT_INT_EQ(wake_align_next_boundary(local(2026, 2, 8, 0, 0, 59)),
                     local(2026, 2, 8, 0, 31, 0));
}

static void test_next_wake_us_at(void)
{
    /* 10:15:00 → 距 10:31:00 恰好 960s */
    int64_t us = wake_align_next_wake_us_at(local(2026, 2, 8, 10, 15, 0)) ;
    MT_ASSERT(us == 960 * 1000000);
    /* 时钟从未校准（epoch < 1e9）→ -1 */
    MT_ASSERT(wake_align_next_wake_us_at((time_t)11101) == -1);
}

int main(void)
{
    /* 与固件一致的本地时区（北京时间）；测试自身也用它构造输入/期望 */
    setenv("TZ", "CST-8", 1);
    tzset();

    MT_RUN(test_hour_start_goes_to_xx01);
    MT_RUN(test_first_half_goes_to_xx31);
    MT_RUN(test_second_half_goes_to_next_hour_xx01);
    MT_RUN(test_midnight_rollover);
    MT_RUN(test_too_close_to_boundary_slides_to_next);
    MT_RUN(test_next_wake_us_at);
    return MT_RESULT();
}
