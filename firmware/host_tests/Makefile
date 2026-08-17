# http_util 宿主机 C 测试：脱离 ESP-IDF 运行时编译运行，一条命令驱动：
#   make            （= make test，编译 + 运行）
#   make clean
#
# 依赖 firmware/managed_components 下的 cJSON（与目标机同一份解析实现），
# 由 idf.py 首次构建/配置时拉取；缺失时先在 firmware/ 下执行一次 idf.py build。

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

TEST_BIN := test_http_util
SRCS := test_http_util.c $(MAIN)/http_util.c $(CJSON)/cJSON.c
HDRS := minitest.h shim/esp_err.h shim/esp_log.h $(MAIN)/http_util.h

.PHONY: test clean
test: $(TEST_BIN)
	./$(TEST_BIN)

$(TEST_BIN): $(SRCS) $(HDRS)
	$(CC) $(CFLAGS) $(CPPFLAGS) -o $@ $(SRCS)

clean:
	rm -f $(TEST_BIN)
