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
import re

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
    """管理台页面以带版本参数的 script 标签引用独立 api() 脚本（B3 seam 落地；
    版本参数破浏览器启发式缓存，api.js 响应无 ETag/Last-Modified）。"""
    html = INDEX.read_text("utf-8")
    assert re.search(r'<script src="/api\.js\?v=\d+"></script>', html), \
        "页面须以 /api.js?v=N 引用独立脚本（版本号随 api.js 内容变更递增）"


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


# ---------- 照片分类（#25：tab 化 + 排序切换 + 时段绑定分类下拉） ----------

def test_webui_photo_library_tab_container():
    """管理台照片库页含分类 tab 容器（全部 + 各分类，横向滚动）。"""
    html = INDEX.read_text("utf-8")
    assert 'id="scoresTabs"' in html, "照片库页须有分类 tab 容器（scoresTabs）"
    assert '当前照片库 tab' in html or 'loadScores' in html


def test_webui_photo_library_sort_switch():
    """分类 tab 内可切换排序（回忆度 / 拍摄时间）。"""
    html = INDEX.read_text("utf-8")
    assert 'id="scoresSort"' in html, "须有排序切换控件"
    assert 'value="memory"' in html and 'value="shot_at"' in html, "排序含回忆度/拍摄时间"


def test_webui_slot_editor_category_dropdown():
    """时段编排里照片时段有分类下拉（含「全部」与照片数提示）。"""
    html = INDEX.read_text("utf-8")
    assert 'tl-cat-sel' in html, "照片时段须展示分类下拉（tl-cat-sel）"
    assert '全部（全库选片）' in html, "分类下拉含「全部」选项"


def test_webui_photo_slot_empty_category_warning():
    """编排里绑定 0 张照片的分类时给出警告（设备将保持上一屏）。"""
    html = INDEX.read_text("utf-8")
    assert "该分类当前" in html, "空分类须有警告文案"
    assert "保持上一屏" in html, "警告须说明设备将保持上一屏"


def test_webui_photo_folder_upload_entry():
    """照片库有上传入口：上传目标跟随选中分类 tab + 多选照片 + 文件夹上传。"""
    html = INDEX.read_text("utf-8")
    assert 'id="frameUploader"' in html, "照片库页须有上传入口容器"
    assert 'webkitdirectory' in html, "须支持按文件夹选择上传（webkitdirectory）"
    assert 'id="uploadTarget"' in html, "须有上传目标显示（跟随选中分类 tab）"
    assert 'newPhotoFolder' in html, "须有新建文件夹入口"


def test_webui_photo_library_split_into_own_tab():
    """照片库功能独立成 tab（#25 后续）：照片库/内容库分离，tab-panel 全对应。"""
    html = INDEX.read_text("utf-8")
    assert 'data-tab="photolib"' in html, "顶部导航须有照片库 tab"
    assert 'id="panel-photolib"' in html, "照片库须有独立 panel"
    assert 'data-tab="library"' in html, "顶部导航须有内容库 tab"
    assert 'id="panel-library"' in html, "内容库须有独立 panel"
    # 每个 data-tab 都有对应 panel（tab 切换通用逻辑不悬空）
    tabs = set(re.findall(r'data-tab="(\w+)"', html))
    panels = set(re.findall(r'id="panel-(\w+)"', html))
    assert tabs <= panels, f"tab 缺 panel: {tabs - panels}"
    # 照片库 panel 含其全部功能区块；内容库 panel 只含上传与网格
    assert 'id="frameUploader"' in html and 'id="scores"' in html
