"""WebUI（管理台 index.html）契约（T6 #16）。

ADR-0002 退役前置：管理台不再引用任何固定 raw/preview 路由——
「✨ 重新生成今日自由模块」按钮走 POST /api/images/free/regenerate，
每日精选面板图片来自内容清单返回的 preview_url。

B3（#21）：api() helper 独立成 web/api.js 并被页面以 script 标签引用
（expand 步：与内联旧 api() 并存，不迁移调用点）。

B5（#23）：裸 fetch 全量迁移——内联旧 api() 删除、页面统一走 fpApi.api，
内容清单改共享单次请求；错误归一样板不再各调用点抄一份。
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


def test_webui_no_bare_fetch():
    """B5：管理台脚本不得裸调 fetch()，一律走 web/api.js 的 api()。

    错误归一样板（读 detail、回退状态文本）只允许住在 api.js 一份。
    """
    html = INDEX.read_text("utf-8")
    assert "fetch(" not in html, "管理台脚本不得出现裸 fetch，HTTP 调用一律走 api()"


def test_webui_uses_fpapi_and_drops_inline_api():
    """B5：页面绑定 api.js 暴露的 fpApi.api，内联旧 api() 定义删除。"""
    html = INDEX.read_text("utf-8")
    assert "fpApi.api" in html, "页面须使用 api.js 暴露的 fpApi.api"
    assert "async function api(path, opts)" not in html, "内联旧 api() 定义应删除"


def test_webui_content_manifest_single_request_call_site():
    """B5：内容清单请求收口为共享 helper 一处，两个面板消费同一份请求。"""
    html = INDEX.read_text("utf-8")
    assert html.count("api('/images/content')") == 1, (
        "内容清单 api() 调用点应唯一（共享单次请求 helper），"
        "面板各自直接请求的形态应消失"
    )
