"""WebUI（管理台 index.html）契约（T6 #16）。

ADR-0002 退役前置：管理台不再引用任何固定 raw/preview 路由——
「✨ 重新生成今日自由模块」按钮走 POST /api/images/free/regenerate，
每日精选面板图片来自内容清单返回的 preview_url。

B3（#21）：api() helper 独立成 web/api.js 并被页面以 script 标签引用
（expand 步：与内联旧 api() 并存，不迁移调用点）。
"""
from pathlib import Path

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

_WEB = Path(__file__).resolve().parents[2] / "services" / "app" / "web"
INDEX = _WEB / "index.html"
API_JS = _WEB / "api.js"

client = TestClient(app)

# ADR-0002 待退役的 10 条固定 raw/preview 路由
# （每日精选/旧称新闻/自由模块/天气/置顶显示 × raw/preview）
_FIXED_ROUTES = [
    "/api/images/daily/raw", "/api/images/daily/preview",
    "/api/images/news/raw", "/api/images/news/preview",
    "/api/images/free/raw", "/api/images/free/preview",
    "/api/images/weather/raw", "/api/images/weather/preview",
    "/api/images/display/raw", "/api/images/display/preview",
]


def test_webui_no_fixed_raw_preview_routes():
    """WebUI 不再引用任何固定 raw/preview 路由（清单 preview_url 已泛化五源）。"""
    html = INDEX.read_text("utf-8")
    stale = [r for r in _FIXED_ROUTES if r in html]
    assert not stale, f"WebUI 不得引用固定 raw/preview 路由: {stale}"


def test_webui_regenerate_button_uses_post_endpoint():
    """重生成按钮走独立管理 POST 端点，不再借 GET raw 端点的 refresh 参数。"""
    html = INDEX.read_text("utf-8")
    assert "/images/free/regenerate" in html, "重生成按钮须走 POST /images/free/regenerate"
    assert "?refresh" not in html, "WebUI 不得再使用 GET raw 的 refresh 参数"


def test_webui_daily_panel_uses_manifest_preview_url():
    """每日精选面板图片来自内容清单条目的 preview_url，不再硬编码固定预览。"""
    html = INDEX.read_text("utf-8")
    assert 'src="${m.preview_url}"' in html, "每日精选面板图片须用清单返回的 preview_url"


def test_webui_references_api_script():
    """管理台页面以 script 标签引用独立 api() 脚本（B3 seam 落地）。"""
    html = INDEX.read_text("utf-8")
    assert '<script src="/api.js"></script>' in html, "页面须引用 /api.js 独立脚本"


def test_api_js_served_as_javascript():
    """GET /api.js 分发脚本本体（text/javascript，内容与 web/api.js 一致）。"""
    r = client.get("/api.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.text == API_JS.read_text("utf-8")
