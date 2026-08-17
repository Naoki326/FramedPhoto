"""B6 #24 — 固件三 client 迁移 http_util，传输管道副本删除。

静态契约（与 test_webui.py 的「无裸 fetch」同一模式，钉住票 #24 验收）：
- content / device / ota 三个 client 的 JSON 请求走 http_util 公共接口；
  grow-buffer 收集器结构体、收集回调、清理函数与 HTTP_TIMEOUT_MS 宏
  副本不得残留（传输管道只允许住在 http_util 一份）；
- 设备标识仅存一份（device_id.h），client 不得自带拷贝；
- 超时差异保留为各调用点的显式参数：内容清单常规 15s、内容大图放宽
  120s、心跳 15s、OTA 60s——不得统一为单一值。
"""
from pathlib import Path

MAIN = Path(__file__).resolve().parents[2] / "firmware" / "main"
CLIENTS = {
    "content": MAIN / "content_client.c",
    "device": MAIN / "device_client.c",
    "ota": MAIN / "ota_client.c",
}

# 手写收集管道的副本特征：结构体 / 追加 / 清理 / 事件收集回调
COLLECTOR_MARKERS = (
    "collect_ctx_t",
    "resp_ctx_t",
    "collect_append",
    "resp_append",
    "collect_free",
    "resp_free",
    "manifest_handler",
    "resp_handler",
)


def _src(name: str) -> str:
    return CLIENTS[name].read_text("utf-8")


def test_clients_route_json_through_http_util():
    """三个 client 的 JSON 请求走 http_util 公共接口（get/post JSON）。"""
    assert "http_util.h" in _src("content"), "content_client 须包含 http_util.h"
    assert "http_util_get_json(" in _src("content"), "内容清单须走 http_util_get_json"
    assert "http_util.h" in _src("device"), "device_client 须包含 http_util.h"
    assert "http_util_post_json(" in _src("device"), "心跳注册须走 http_util_post_json"
    assert "http_util.h" in _src("ota"), "ota_client 须包含 http_util.h"
    assert "http_util_get_json(" in _src("ota"), "OTA manifest 须走 http_util_get_json"


def test_no_grow_buffer_collector_copies_in_clients():
    """收集器结构体 / 收集回调 / 清理函数副本不得在 client 残留。"""
    for name, path in CLIENTS.items():
        text = path.read_text("utf-8")
        leaked = [m for m in COLLECTOR_MARKERS if m in text]
        assert not leaked, f"{name}_client.c 不得残留手写收集管道副本: {leaked}"


def test_no_http_timeout_macro_copies():
    """HTTP_TIMEOUT_MS 宏副本删除；超时只能以调用点参数出现。"""
    for name, path in CLIENTS.items():
        assert "#define HTTP_TIMEOUT_MS" not in path.read_text("utf-8"), (
            f"{name}_client.c 不得再定义超时宏副本"
        )


def test_device_id_single_copy_in_dedicated_header():
    """设备标识仅存 device_id.h 一份，client 不自带拷贝。"""
    files = sorted(MAIN.glob("*.c")) + sorted(MAIN.glob("*.h"))
    holders = [
        f.name
        for f in files
        if "esp32s3-%02x%02x%02x" in f.read_text("utf-8")
    ]
    assert holders == ["device_id.h"], (
        f"设备标识格式串应仅存 device_id.h 一份: {holders}"
    )
    for name in CLIENTS:
        assert "esp_read_mac" not in _src(name), (
            f"{name}_client.c 应经 device_id.h 取标识，不得直接读 MAC"
        )


def test_timeout_tiers_kept_as_caller_arguments():
    """现有超时差异以调用点参数保留，未统一为单一值。"""
    content = _src("content")
    assert "15000" in content, "内容清单常规超时 15s 应在调用点显式出现"
    assert "120000" in content, "内容大图放宽超时 120s 应在调用点显式出现"
    assert "15000" in _src("device"), "心跳超时 15s 应在调用点显式出现"
    assert "60000" in _src("ota"), "OTA 最宽超时 60s 应在调用点显式出现"
