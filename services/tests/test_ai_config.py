"""管理台「AI 模型」页（/api/ai）契约测试。

覆盖三件事：
1. 密钥掩码语义——GET 只回掩码、PUT 留空即不改、"-" 显式清空、
   覆盖为空串时不再回退 .env（管理台是这几个 key 的唯一权威）；
2. 消费点即时生效——保存后 analyzer / llm / 即梦等读到新值，
   无需重启（runtime_config 覆盖优先于 .env）；
3. 校验与连通性测试端点——枚举字段非法值 400，测试端点不抛异常、
   按 target 分派（vlm / llm / image）。
"""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import ai_secrets
from app import runtime_config
from app.config import settings
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_runtime_config(monkeypatch, tmp_path):
    """隔离 runtime_config（不写真实 services/app/runtime_config.json）。"""
    monkeypatch.setattr(runtime_config, "CONFIG_PATH", tmp_path / "runtime_config.json")


def _get_config() -> dict:
    r = client.get("/api/ai")
    assert r.status_code == 200, r.text
    return r.json()["config"]


def _get_status() -> dict:
    return client.get("/api/ai").json()["status"]


# ---------- 掩码语义 ----------

def test_get_never_returns_plaintext_secret():
    """密钥只回掩码：前 6 位 + 末 4 位，中间以 … 省略，不足以还原原值。"""
    masked = _get_config()["vlm_api_key"]
    assert "…" in masked
    assert masked[:6] == str(settings.vlm_api_key)[:6]
    assert masked[-4:] == str(settings.vlm_api_key)[-4:]
    assert str(settings.vlm_api_key) not in json.dumps(_get_config())


def test_mask_short_and_empty_values():
    """掩码边界：空值空串；短值只给前缀，不泄漏全量。"""
    assert ai_secrets.mask("") == ""
    assert ai_secrets.mask(None) == ""
    assert ai_secrets.mask("abc") == "abc…"


def test_secret_keys_are_registered_as_secrets():
    """密钥字段集合与白名单交集非空且覆盖全部接入点（防止新 key 漏掩码）。"""
    assert {"vlm_api_key", "anthropic_api_key", "text_api_key",
            "image_api_key", "image_signed_ak", "image_signed_sk",
            "weather_key"} <= set(runtime_config.SECRET_KEYS)
    assert runtime_config.SECRET_KEYS <= set(runtime_config.ALLOWED_KEYS)


def test_put_empty_secret_keeps_previous_value():
    """留空（含掩码原样回传）→ 不修改：页面整表提交也不会误清密钥。"""
    masked = _get_config()["weather_key"]
    r = client.put("/api/ai", json={"weather_key": masked})   # 掩码原样提交
    assert r.status_code == 200
    assert r.json()["saved"] == {}
    r = client.put("/api/ai", json={"weather_key": ""})       # 空串
    assert r.json()["saved"] == {}
    assert _get_config()["weather_key"] == masked


def test_put_new_value_updates_and_masks_response():
    """新值写入 runtime_config；响应里只回 *** 占位，不回明文。"""
    r = client.put("/api/ai", json={"text_api_key": "sk-brand-new-123456"})
    assert r.status_code == 200, r.text
    assert r.json()["saved"]["text_api_key"] == "***"
    assert "sk-brand-new-123456" not in r.text
    assert runtime_config.get("text_api_key") == "sk-brand-new-123456"
    assert _get_config()["text_api_key"] == "sk-bra…3456"


def test_put_dash_clears_override_not_fallback_to_env(monkeypatch):
    """填 "-" = 置空：runtime_config 落空串，不再回退 .env——
    否则 .env 里有 key 时用户在 UI 里永远关不掉它。"""
    monkeypatch.setattr(settings, "weather_key", "env-key-value-0001")
    client.put("/api/ai", json={"weather_key": "ui-key-value-0002"})
    assert _get_config()["weather_key"] != ""
    r = client.put("/api/ai", json={"weather_key": ai_secrets.CLEAR})
    assert r.json()["saved"]["weather_key"] == "***"
    assert runtime_config.get("weather_key") == ""
    assert _get_config()["weather_key"] == ""
    assert _get_config()["weather_key_set"] is False
    assert _get_status()["weather_ready"] is False


# ---------- 消费点即时生效（runtime_config 覆盖优先）----------

def test_saved_config_effective_immediately_in_consumers():
    """保存后各消费点现读现用：llm 文本/图片配置、analyzer 的 VLM 客户端。"""
    from app import llm
    from app.analyzer import build_client, OpenAICompatClient

    client.put("/api/ai", json={
        "text_api_key": "ui-text-0001", "text_model": "glm-ui",
        "image_ratio": "16:9", "vlm_provider": "openai",
        "vlm_api_mode": "chat", "vlm_api_url": "http://127.0.0.1:1234/v1",
        "vlm_model": "qwen-ui", "vlm_timeout": 42, "vlm_enabled": True,
    })
    assert llm.text_key() == "ui-text-0001"
    assert llm.text_model() == "glm-ui"
    assert llm.image_ratio() == "16:9"
    c = build_client()
    assert isinstance(c, OpenAICompatClient)
    assert (c.url, c.model, c.timeout) == ("http://127.0.0.1:1234/v1", "qwen-ui", 42)


def test_vlm_proxy_override_reaches_requests(monkeypatch):
    """代理经 analyzer._proxies 生效（requests 的 proxies 参数）。"""
    from app import analyzer
    client.put("/api/ai", json={"vlm_proxy": "http://127.0.0.1:9999"})
    assert analyzer._proxies() == {"http": "http://127.0.0.1:9999",
                                  "https": "http://127.0.0.1:9999"}
    client.put("/api/ai", json={"vlm_proxy": ""})
    assert analyzer._proxies() is None


def test_vlm_disabled_override_skips_vlm(monkeypatch):
    """vlm_enabled=false 时 analyze_image 直接走启发式（不建客户端）。"""
    from app import analyzer
    client.put("/api/ai", json={"vlm_enabled": False, "vlm_enabled_sentinel": None})
    called: list = []
    monkeypatch.setattr(analyzer, "build_client", lambda: called.append(1))
    monkeypatch.setattr(analyzer, "heuristic_score", lambda p: (60.0, 60.0, "启发式"))
    a = analyzer.analyze_image(__file__)
    assert a.source == "heuristic"
    assert not called, "VLM 关闭时不应构建客户端"


def test_nas_config_effective_immediately():
    """NAS 接入配置即时生效（nas_sync 不再直接读 settings）。"""
    from app import nas_sync
    client.put("/api/ai", json={
        "nas_ssh_host": "nas-ui", "nas_ssh_user": "ui-user",
        "nas_photo_dir": "/volume1/ui-photo/", "nas_ssh_port": 2222,
    })
    assert nas_sync.configured()
    assert nas_sync.nas_photo_dir() == "/volume1/ui-photo"
    assert nas_sync._target() == "ui-user@nas-ui"
    assert "2222" in nas_sync._ssh_base()


def test_weather_key_override_reaches_weather_card(monkeypatch):
    """和风 key/host 即时生效，并触发天气缓存清理（下一时段用新配置渲染）。"""
    from app import weather_card
    from app.routers import settings as settings_router
    cleared: list = []
    monkeypatch.setattr(settings_router, "_clear_weather_cache", lambda: cleared.append(1))
    client.put("/api/ai", json={"weather_key": "ui-qw-key", "weather_host": "ui.host"})
    assert cleared, "天气 key/host 变化应清天气缓存"
    seen: dict = {}

    def _fake_get(url, **kwargs):
        seen["url"] = url
        seen["params"] = kwargs.get("params")

        class _R:
            @staticmethod
            def json():
                return {"code": "200", "now": {"temp": "20"}, "daily": []}
        return _R()

    monkeypatch.setattr(weather_card.requests, "get", _fake_get)
    monkeypatch.setattr(weather_card, "resolve_location", lambda: ("101020300", "上海"))
    assert weather_card.fetch_weather() is not None
    assert seen["url"] == "https://ui.host/v7/weather/3d"   # now 与 3d 同 base，取末次
    assert seen["params"]["key"] == "ui-qw-key"


# ---------- 校验 ----------

@pytest.mark.parametrize("payload", [
    {"vlm_provider": "nope"},
    {"vlm_api_mode": "stream"},
    {"image_ratio": "1:1"},
    {"vlm_timeout": 0},
])
def test_invalid_enum_values_rejected(payload):
    r = client.put("/api/ai", json=payload)
    assert r.status_code == 400, r.text


def test_unknown_fields_ignored():
    """未注册字段不入库（管理台不会悄悄塞进 runtime_config）。"""
    r = client.put("/api/ai", json={"totally_unknown": "x", "vlm_model": "m1"})
    assert r.status_code == 200
    assert "totally_unknown" not in runtime_config.load()
    assert runtime_config.get("vlm_model") == "m1"


# ---------- 连通性测试端点 ----------

def test_test_endpoint_reports_vlm_unconfigured(monkeypatch):
    """VLM 未配置 → ok=False 且给出可读原因（不抛 500）。"""
    monkeypatch.setattr("app.analyzer.build_client", lambda: None)
    r = client.post("/api/ai/test", json={"target": "vlm"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "未配置" in body["detail"]


def test_test_endpoint_vlm_success(monkeypatch):
    """连通性成功：返回类名、样例文本与耗时。"""
    class _FakeClient:
        def generate_caption(self, _raw: bytes) -> str:
            return "  一张白纸  "

    monkeypatch.setattr("app.analyzer.build_client", lambda: _FakeClient())
    body = client.post("/api/ai/test", json={"target": "vlm"}).json()
    assert body["ok"] is True
    assert "响应正常" in body["detail"]
    assert body["sample"] == "一张白纸"
    assert isinstance(body["elapsed_ms"], int)


def test_test_endpoint_vlm_failure_is_reported(monkeypatch):
    """连通性失败：把异常类名与消息回给页面，而不是 500。"""
    class _Boom:
        def generate_caption(self, _raw: bytes) -> str:
            raise ConnectionError("proxy refused")

    monkeypatch.setattr("app.analyzer.build_client", lambda: _Boom())
    body = client.post("/api/ai/test", json={"target": "vlm"}).json()
    assert body["ok"] is False
    assert "ConnectionError" in body["detail"]


def test_test_endpoint_llm_without_key():
    client.put("/api/ai", json={"text_api_key": ai_secrets.CLEAR})
    body = client.post("/api/ai/test", json={"target": "llm"}).json()
    assert body["ok"] is False
    assert "未配置" in body["detail"]


def test_test_endpoint_image_reports_chain(monkeypatch):
    """文生图只检查链路就绪性（不实际出图：按次计费）。"""
    monkeypatch.setattr("app.llm.image_signed_conf", lambda: ("ak", "sk"))
    body = client.post("/api/ai/test", json={"target": "image"}).json()
    assert body["ok"] is True
    assert "签名式异步接口" in body["detail"]
    monkeypatch.setattr("app.llm.image_signed_conf", lambda: ("", ""))
    monkeypatch.setattr("app.llm.image_conf", lambda: ("http://x/v1", "k", "m"))
    body = client.post("/api/ai/test", json={"target": "image"}).json()
    assert body["ok"] is True
    assert "OpenAI 兼容图片接口" in body["detail"]
    monkeypatch.setattr("app.llm.image_conf", lambda: ("", "", ""))
    assert client.post("/api/ai/test", json={"target": "image"}).json()["ok"] is False


def test_test_endpoint_rejects_unknown_target():
    assert client.post("/api/ai/test", json={"target": "nope"}).status_code == 400


def test_calendar_quote_endpoint(monkeypatch):
    """寄语调试端点：返回当日寄语与所用模型（force 清缓存重生成）。

    无 key 时走内置语料降级（不真调外部 API），缓存照常落盘。
    """
    import datetime
    from app import calendar_card
    client.put("/api/ai", json={"text_api_key": ai_secrets.CLEAR,
                               "text_model": "glm-ui"})
    body = client.get("/api/ai/calendar-quote").json()
    assert body["quote"]
    assert body["model"] == "glm-ui"
    assert body["llm_ready"] is False
    assert calendar_card._quote_file(datetime.date.today()).exists()


def test_calendar_quote_force_regenerates(monkeypatch):
    """force=true 清掉当天缓存重新生成（管理台可反复试 LLM 效果）。"""
    calls: list = []
    monkeypatch.setattr("app.calendar_card._quote_llm",
                        lambda t, c: calls.append(1) or f"第{len(calls)}次")
    client.put("/api/ai", json={"text_api_key": "ui-key"})
    assert client.get("/api/ai/calendar-quote?force=true").json()["quote"] == "第1次"
    assert client.get("/api/ai/calendar-quote?force=true").json()["quote"] == "第2次"
    # 不带 force：命中当日缓存，不再调 LLM
    assert client.get("/api/ai/calendar-quote").json()["quote"] == "第2次"
    assert len(calls) == 2


# ---------- 旧厂商名键兼容（改名后历史配置仍可用）----------

def test_legacy_runtime_config_keys_resolve_to_neutral(monkeypatch):
    """历史 runtime_config.json 里的厂商名键经别名读到中性键：用户不必手工改文件。"""
    runtime_config.CONFIG_PATH.write_text(json.dumps({
        "zhipu_api_key": "legacy-text-key",
        "zhipu_model": "legacy-model",
        "jimeng_access_key": "legacy-ak",
        "jimeng_secret_key": "legacy-sk",
        "jimeng_ratio": "16:9",
        "imagegen_url": "http://legacy/v1/images",
        "imagegen_model": "legacy-img",
        "qweather_key": "legacy-weather",
        "qweather_host": "legacy.host",
    }), encoding="utf-8")
    c = _get_config()
    # 掩码形态与 ai_secrets.mask 一致（不因走别名而回显明文）
    assert c["text_api_key"] == ai_secrets.mask("legacy-text-key")
    assert c["text_api_key"] == "legacy…-key"
    assert "legacy-text-key" not in json.dumps(c)
    assert runtime_config.get("text_model") == "legacy-model"
    assert runtime_config.get("image_signed_ak") == "legacy-ak"
    assert runtime_config.get("image_ratio") == "16:9"
    assert runtime_config.get("image_api_url") == "http://legacy/v1/images"
    assert runtime_config.get("weather_key") == "legacy-weather"
    st = _get_status()
    assert st["text_ready"] and st["weather_ready"]


def test_new_key_wins_over_legacy_in_runtime_config():
    """同时存在新旧键时新键优先（与管理台保存后的状态一致）。"""
    runtime_config.CONFIG_PATH.write_text(json.dumps({
        "zhipu_model": "legacy-model", "text_model": "new-model",
    }), encoding="utf-8")
    assert runtime_config.get("text_model") == "new-model"


def test_legacy_env_keys_still_readable(monkeypatch):
    """历史 .env 的厂商名键仍能读到（新键优先）；纯旧配置也能推出文生图链路。"""
    from app.config import Settings
    for key in ("TEXT_API_KEY", "TEXT_MODEL", "IMAGE_API", "IMAGE_API_URL",
                "IMAGE_API_KEY", "IMAGE_MODEL", "IMAGE_SIGNED_AK", "IMAGE_SIGNED_SK",
                "WEATHER_KEY", "WEATHER_HOST"):
        monkeypatch.delenv(key, raising=False)
    s = Settings(
        zhipu_api_key="env-legacy-text", zhipu_model="env-legacy-model",
        jimeng_access_key="env-ak", jimeng_secret_key="env-sk",
        qweather_key="env-weather", qweather_host="env.host",
    )
    assert (s.text_api_key, s.text_model) == ("env-legacy-text", "env-legacy-model")
    assert (s.image_signed_ak, s.image_signed_sk) == ("env-ak", "env-sk")
    assert (s.weather_key, s.weather_host) == ("env-weather", "env.host")
    # 只配了签名式凭据 → provider 自动落到签名式（旧配置不因改名而失效）
    assert s.image_api == "signed_async"


def test_explicit_new_provider_wins_over_legacy_credentials():
    """显式 IMAGE_API=openai 时不被旧签名凭据改写 provider。"""
    from app.config import Settings
    s = Settings(image_api="openai", image_api_url="http://new/v1",
                 jimeng_access_key="old-ak", jimeng_secret_key="old-sk")
    assert s.image_api == "openai"
    assert s.image_api_url == "http://new/v1"


# ---------- 配置面不绑厂家（防回归）----------

#: 允许出现厂商名的三处：兼容层（读旧键）、适配层（签名协议）、厂商 MCP 客户端
_ALLOWED_FILES = {"config.py", "runtime_config.py", "image_signed.py", "zhipu_mcp.py"}

#: 厂商名以配置键形式出现的模式（属性读取 / 环境变量 / 字典键）。
#: 域名常量（如 geoapi.qweather.com）与文档文本不算——那些是接口事实，
#: 不是「配置面绑厂家」的耦合（换厂家本就要改地址）。
_VENDOR_KEY_RE = re.compile(
    r"\b(?:settings\.|self\.)?"
    r"((?:zhipu|jimeng|imagegen|qweather)_(?:api_key|model|access_key|secret_key|ratio|url|key|host|city|location))\b",
    re.IGNORECASE,
)


def _vendor_key_hits(text: str) -> list[str]:
    """文本里以配置键形态出现的厂商名（大小写不敏感，去重保序）。"""
    hits: list[str] = []
    for m in _VENDOR_KEY_RE.finditer(text):
        token = m.group(1)
        if token not in hits:
            hits.append(token)
    return hits


def test_consumers_do_not_read_vendor_named_keys():
    """消费点（业务模块）只经 app.llm / 中性键取值，不得再出现厂商名配置键。

    名字一旦散回业务代码，换厂家就要改多处；这条测试把「配置面是抽象的」
    钉住：新增消费点若直接读 ZHIPU_API_KEY 之类，会在这里失败。
    """
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []
    for path in sorted(app_dir.rglob("*.py")):
        if path.name in _ALLOWED_FILES or path.parent.name == "web":
            continue
        hits = _vendor_key_hits(path.read_text(encoding="utf-8"))
        if hits:
            offenders.append(f"{path.name}: {hits}")
    assert not offenders, "业务模块出现厂商名配置键（应走 app/llm.py 的中性取值）：" + "; ".join(offenders)


def test_ui_does_not_expose_vendor_names():
    """管理台页面不出现厂商名配置键（配置项按接口形态描述，换厂家不改 UI）。

    内部 id（如 aiQwHost）与域名占位（devapi.qweather.com）不在此列。
    """
    web = Path(__file__).resolve().parents[1] / "app" / "web"
    offenders: list[str] = []
    for name in ("index.html", "api.js"):
        hits = _vendor_key_hits((web / name).read_text(encoding="utf-8"))
        if hits:
            offenders.append(f"{name}: {hits}")
    assert not offenders, "管理台出现厂商名配置键：" + "; ".join(offenders)


def test_vendor_key_detector_catches_regressions():
    """探测器本身有效（正例命中、域名与内部 id 不误报）。"""
    assert _vendor_key_hits('settings.qweather_key') == ["qweather_key"]
    assert _vendor_key_hits('runtime_config.effective("zhipu_api_key", s)')
    assert _vendor_key_hits('{"jimeng_secret_key": 1}')
    assert _vendor_key_hits("https://geoapi.qweather.com/v2/city/lookup") == []
    assert _vendor_key_hits("id=\"aiQwHost\"") == []


def test_status_reports_provider_by_interface_shape():
    """状态回显按接口形态给值（页面据此选 toggle 分支），不带厂商语义。"""
    client.put("/api/ai", json={"text_api": "anthropic", "image_api": "openai",
                               "image_api_url": "http://x/v1", "image_api_key": "k",
                               "image_model": "m", "image_ratio": "16:9"})
    st = _get_status()
    assert st["text_api"] == "anthropic"
    assert st["image_api"] == "openai"
    assert st["image_ready"] is True
    assert st["image_openai_ready"] is True
    assert st["image_signed_ready"] is False
    assert st["image_ratio"] == "16:9"


def test_text_anthropic_endpoint_shape():
    """/v1/messages 端点按 Anthropic 兼容形态拼接（base 或完整地址都能用）。"""
    from app import llm as llm_mod
    client.put("/api/ai", json={"text_api": "anthropic",
                               "text_base_url": "https://api.anthropic.test"})
    assert llm_mod.text_chat_url() == "https://api.anthropic.test/v1/messages"
    client.put("/api/ai", json={"text_base_url": "https://x.test/v1/messages"})
    assert llm_mod.text_chat_url() == "https://x.test/v1/messages"
    client.put("/api/ai", json={"text_api": "openai", "text_base_url": "https://y.test/v1"})
    assert llm_mod.text_chat_url() == "https://y.test/v1/chat/completions"
