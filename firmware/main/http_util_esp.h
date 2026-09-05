/*
 * http_util_esp.h — http_util 的 esp_http_client 传输后端（固件专用）
 */
#pragma once

/* 显式安装 esp_http_client 传输后端（app_main 启动时调用一次）。
 *
 * 真机教训（0.1.9）：曾依赖"强符号覆盖 http_util.c 弱默认"的链接期机制，
 * 但 libmain.a 归档成员是否被拉入取决于布局 —— 新增一个源文件（wake_align.c）
 * 后链接器不再拉入 http_util_esp.o，全部 HTTP 请求瞬间 ESP_ERR_NOT_SUPPORTED，
 * 设备失联。后端必须运行时显式安装，不依赖链接器行为。 */
void http_util_esp_install(void);
