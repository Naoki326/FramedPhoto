/*
 * minitest.h — 极简断言头（Unity 的零依赖等价物，随本票选型）。
 * 单个测试翻译单元内使用：MT_RUN 逐个执行，main 返回 MT_RESULT()。
 */
#ifndef MINITEST_H
#define MINITEST_H

#include <stdio.h>
#include <string.h>

static int mt_failures = 0;

#define MT_ASSERT(cond)                                                     \
    do {                                                                    \
        if (!(cond)) {                                                      \
            fprintf(stderr, "  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            mt_failures++;                                                  \
        }                                                                   \
    } while (0)

#define MT_ASSERT_INT_EQ(got, want)                                         \
    do {                                                                    \
        long mt_got_ = (long)(got), mt_want_ = (long)(want);                \
        if (mt_got_ != mt_want_) {                                          \
            fprintf(stderr, "  FAIL %s:%d: %s == %ld, want %ld (%s)\n",     \
                    __FILE__, __LINE__, #got, mt_got_, mt_want_, #want);    \
            mt_failures++;                                                  \
        }                                                                   \
    } while (0)

#define MT_ASSERT_STR_EQ(got, want)                                         \
    do {                                                                    \
        const char *mt_got_ = (got), *mt_want_ = (want);                    \
        if (!mt_got_ || !mt_want_ || strcmp(mt_got_, mt_want_) != 0) {      \
            fprintf(stderr, "  FAIL %s:%d: %s == \"%s\", want \"%s\"\n",    \
                    __FILE__, __LINE__, #got,                               \
                    mt_got_ ? mt_got_ : "(null)", mt_want_);                \
            mt_failures++;                                                  \
        }                                                                   \
    } while (0)

#define MT_RUN(fn)                                                          \
    do {                                                                    \
        printf("== %s\n", #fn);                                             \
        fn();                                                               \
    } while (0)

#define MT_RESULT() (mt_failures > 0)

#endif /* MINITEST_H */
