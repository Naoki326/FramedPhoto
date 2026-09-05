# 宿主机 C 测试：脱离 ESP-IDF 运行时编译运行，一条命令驱动：
#   make            （= make test，编译 + 运行）
#   make clean
#
# 依赖 firmware/managed_components 下的 cJSON（与目标机同一份解析实现），
# 由 idf.py 首次构建/配置时拉取；缺失时先在 firmware/ 下执行一次 idf.py build。
#
# wake_align 测试是纯 libc 逻辑（localtime/mktime + TZ），不依赖 cJSON。

ROOT    := ..
MAIN    := $(ROOT)/main
CJSON   := $(ROOT)/managed_components/espressif__cjson/cJSON

ifeq ($(wildcard $(CJSON)/cJSON.c),)
$(error cJSON not found under managed_components; run idf.py once in firmware/ to fetch dependencies)
endif

CC      ?= cc
ASAN    := $(shell $(CC) -fsanitize=address -x c /dev/null -c -o /dev/null 2>/dev/null && echo yes)
CFLAGS  ?= -std=c11 -Wall -Wextra -g $(if $(filter yes,$(ASAN)),-fsanitize=address,)
CPPFLAGS := -I$(MAIN) -Ishim -I$(CJSON) -I.
# 被测代码引用的 CONFIG_ 项（正式值来自 ESP-IDF 的 sdkconfig.h，宿主这里显式给定）
CPPFLAGS += -DCONFIG_FRAMEDPHOTO_ALIGN_WAKE=1 -DCONFIG_FRAMEDPHOTO_UTC_OFFSET_HOURS=8

HTTP_SRCS := test_http_util.c $(MAIN)/http_util.c $(CJSON)/cJSON.c
WAKE_SRCS := test_wake_align.c $(MAIN)/wake_align.c
HDRS      := minitest.h shim/esp_err.h shim/esp_log.h shim/sdkconfig.h \
             $(MAIN)/http_util.h $(MAIN)/wake_align.h

.PHONY: test clean
test: test_http_util test_wake_align
	./test_http_util
	./test_wake_align

test_http_util: $(HTTP_SRCS) $(HDRS)
	$(CC) $(CFLAGS) $(CPPFLAGS) -o $@ $(HTTP_SRCS)

test_wake_align: $(WAKE_SRCS) $(HDRS)
	$(CC) $(CFLAGS) $(CPPFLAGS) -o $@ $(WAKE_SRCS)

clean:
	rm -f test_http_util test_wake_align
