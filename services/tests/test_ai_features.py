"""AI 分析 / 每日精选 / 文案渲染 测试。"""
import datetime as dt
import io
import os
import tempfile

import pytest
from PIL import Image

from app import db, daily
from app.analyzer import (
    AnthropicClient,
    OpenAICompatClient,
    OpenAIResponsesClient,
    VLMUnavailable,
    analyze_image,
    build_client,
    extract_exif,
    heuristic_score,
)
from app.epd_image import prepare_image_with_caption

_tmp = tempfile.mkdtemp(prefix="framedphoto-ai-test-")


# ---------- 启发式评分 ----------

def _make_photo(path: str, size=(1200, 1600), color=(120, 90, 200)) -> str:
    img = Image.new("RGB", size, color)
    img.save(path, format="PNG")
    return path


def test_heuristic_score_basic():
    p = _make_photo(os.path.join(_tmp, "basic.png"))
    mem, beauty, reason = heuristic_score(p)
    assert 0 <= mem <= 100 and 0 <= beauty <= 100
    assert reason


def test_heuristic_invalid_file():
    p = os.path.join(_tmp, "bad.png")
    with open(p, "w") as f:
        f.write("not an image")
    mem, beauty, _ = heuristic_score(p)
    assert 0 <= mem <= 100 and 0 <= beauty <= 100  # 不抛异常


def test_extract_exif_time():
    # PIL 写 EXIF 拍摄时间
    img = Image.new("RGB", (10, 10))
    exif = Image.Exif()
    exif[0x0132] = "2023:05:14 16:30:22"
    p = os.path.join(_tmp, "exif.png")
    img.save(p, exif=exif)
    shot_at, lat, lon, source = extract_exif(p)
    assert shot_at is not None
    assert source == "exif"
    t = dt.datetime.fromtimestamp(shot_at, tz=dt.timezone.utc)
    assert (t.year, t.month, t.day) == (2023, 5, 14)
    assert lat is None and lon is None


def test_extract_exif_fallback_mtime():
    p = _make_photo(os.path.join(_tmp, "noexif.png"))
    shot_at, _, _, source = extract_exif(p)
    assert shot_at is not None  # 兜底用文件 mtime
    assert source == "file"


# ---------- VLM 调用 ----------

def test_vlm_unavailable_without_config(monkeypatch):
    monkeypatch.setattr("app.analyzer.settings.vlm_provider", "openai")
    monkeypatch.setattr("app.analyzer.settings.vlm_api_url", "")
    with pytest.raises(VLMUnavailable):
        OpenAICompatClient("", "", "m", 10).analyze(b"fake-image")


def test_analyze_image_heuristic_when_vlm_disabled(monkeypatch):
    p = _make_photo(os.path.join(_tmp, "hv.png"))
    monkeypatch.setattr("app.analyzer.settings.vlm_enabled", False)
    a = analyze_image(p, use_vlm=False)
    assert a.source == "heuristic"
    assert a.path == p
    assert 0 <= a.memory_score <= 100
    assert a.filename == "hv.png"


def test_analyze_image_vlm_fallback(monkeypatch):
    """VLM 失败时降级启发式，不抛异常。"""
    p = _make_photo(os.path.join(_tmp, "vf.png"))
    monkeypatch.setattr("app.analyzer.settings.vlm_enabled", True)
    monkeypatch.setattr("app.analyzer.settings.vlm_provider", "openai")
    monkeypatch.setattr("app.analyzer.settings.vlm_api_url", "http://127.0.0.1:1/v1")
    a = analyze_image(p, use_vlm=True)
    assert a.source == "heuristic"
    assert "VLM 不可用" in a.reason


def test_analyze_image_vlm_success(monkeypatch):
    p = _make_photo(os.path.join(_tmp, "vs.png"))
    monkeypatch.setattr("app.analyzer.settings.vlm_enabled", True)

    class FakeClient:
        def analyze(self, raw):
            return {
                "description": "海边日落",
                "type": "风景",
                "memory_score": 88,
                "beauty_score": 90,
                "caption": "那天傍晚的海",
                "reason": "光影很美",
            }
    monkeypatch.setattr("app.analyzer.build_client", lambda: FakeClient())
    a = analyze_image(p, use_vlm=True)
    assert a.source == "vlm"
    assert a.memory_score == 88
    assert a.caption == "那天傍晚的海"


# ---------- Anthropic 客户端 ----------

def _fake_anthropic_response(text: str):
    class FakeResp:
        def raise_for_status(self):
            pass
        def json(self):
            return {"content": [{"text": text}]}
    return FakeResp()


def test_anthropic_request_format(monkeypatch):
    """Anthropic 请求：/v1/messages、x-api-key 头、image base64 结构。"""
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _fake_anthropic_response('{"description":"猫","type":"宠物","memory_score":80,"beauty_score":70,"caption":"我家猫","reason":"可爱"}')

    monkeypatch.setattr("app.analyzer.requests.post", fake_post)
    client = AnthropicClient("sk-ant-test", "claude-sonnet-4-20250514", 60)
    result = client.analyze(b"fake-image")

    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["json"]["model"] == "claude-sonnet-4-20250514"
    assert captured["json"]["system"]  # 提示词
    img = captured["json"]["messages"][0]["content"][0]
    assert img["type"] == "image"
    assert img["source"]["type"] == "base64"
    assert img["source"]["media_type"] == "image/jpeg"
    assert img["source"]["data"]
    assert result["caption"] == "我家猫"


def test_anthropic_no_key_raises():
    with pytest.raises(VLMUnavailable):
        AnthropicClient("", "m", 10).analyze(b"x")


def test_provider_auto_prefers_anthropic(monkeypatch):
    monkeypatch.setattr("app.analyzer.settings.vlm_provider", "auto")
    monkeypatch.setattr("app.analyzer.settings.anthropic_api_key", "sk-ant-test")
    monkeypatch.setattr("app.analyzer.settings.vlm_api_url", "http://127.0.0.1:1234/v1")
    client = build_client()
    assert isinstance(client, AnthropicClient)


def test_provider_openai_explicit(monkeypatch):
    monkeypatch.setattr("app.analyzer.settings.vlm_provider", "openai")
    monkeypatch.setattr("app.analyzer.settings.vlm_api_mode", "chat")
    monkeypatch.setattr("app.analyzer.settings.anthropic_api_key", "sk-ant-test")
    monkeypatch.setattr("app.analyzer.settings.vlm_api_url", "http://127.0.0.1:1234/v1")
    client = build_client()
    assert isinstance(client, OpenAICompatClient)


def test_provider_disabled_returns_none(monkeypatch):
    monkeypatch.setattr("app.analyzer.settings.vlm_provider", "disabled")
    assert build_client() is None


# ---------- Responses API 客户端 ----------

def test_responses_request_format(monkeypatch):
    """Responses API：/responses 端点、input_image 结构、instructions 提示词。"""
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, proxies=None):
        captured["url"] = url
        captured["json"] = json
        captured["proxies"] = proxies
        class FakeResp:
            def raise_for_status(self):
                pass
            def json(self):
                return {"output": [{
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": (
                        '{"description":"夜景","type":"城市","memory_score":77,'
                        '"beauty_score":80,"caption":"灯影","reason":"氛围好"}')}],
                }]}
        return FakeResp()

    monkeypatch.setattr("app.analyzer.requests.post", fake_post)
    monkeypatch.setattr("app.analyzer.settings.vlm_proxy", "http://127.0.0.1:7897")

    client = OpenAIResponsesClient("https://opencode.ai/zen/go/v1/chat/completions",
                                   "sk-test", "gpt-5.6-luna", 60)
    result = client.analyze(b"fake-image")

    # chat/completions 完整 URL 应被规范为 /responses
    assert captured["url"] == "https://opencode.ai/zen/go/v1/responses"
    assert captured["json"]["model"] == "gpt-5.6-luna"
    assert captured["json"]["instructions"]  # system 提示词
    item = captured["json"]["input"][0]["content"][0]
    assert item["type"] == "input_image"
    assert item["image_url"].startswith("data:image/jpeg;base64,")
    assert captured["proxies"]["https"] == "http://127.0.0.1:7897"
    assert result["caption"] == "灯影"


def test_responses_empty_content_raises(monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None, proxies=None):
        class FakeResp:
            def raise_for_status(self):
                pass
            def json(self):
                return {"output": [{"type": "reasoning", "content": []}]}
        return FakeResp()
    monkeypatch.setattr("app.analyzer.requests.post", fake_post)
    with pytest.raises(VLMUnavailable):
        OpenAIResponsesClient("https://x/v1", "k", "m", 60).analyze(b"x")


def test_provider_responses_mode(monkeypatch):
    monkeypatch.setattr("app.analyzer.settings.vlm_provider", "openai")
    monkeypatch.setattr("app.analyzer.settings.vlm_api_mode", "responses")
    monkeypatch.setattr("app.analyzer.settings.vlm_api_url", "https://opencode.ai/zen/go/v1")
    client = build_client()
    assert isinstance(client, OpenAIResponsesClient)
    assert client.url == "https://opencode.ai/zen/go/v1/responses"


# ---------- 文案渲染 ----------

def test_prepare_image_with_caption_layout():
    buf = io.BytesIO()
    Image.new("RGB", (1200, 1600), (200, 100, 50)).save(buf, format="PNG")
    p = prepare_image_with_caption(buf.getvalue(), caption="纪念我们一起旅行的日子", date_str="2023.05.14")
    assert p.width == 1200 and p.height == 1600
    assert len(p.data) == 20 + 960000
    # 底部文案区应比纯照片版本更暗（渐变带）
    p2 = prepare_image_with_caption(buf.getvalue(), caption="", date_str="")
    bottom = p.data[20 + 1599 * 600:20 + 1600 * 600]
    bottom2 = p2.data[20 + 1599 * 600:20 + 1600 * 600]
    assert bottom != bottom2  # 底部有文字/渐变覆盖


def test_prepare_image_with_caption_empty_is_plain():
    buf = io.BytesIO()
    Image.new("RGB", (1200, 1600), (200, 100, 50)).save(buf, format="PNG")
    a = prepare_image_with_caption(buf.getvalue(), caption="", date_str="")
    from app.epd_image import prepare_image
    b = prepare_image(buf.getvalue(), dither=True)
    assert a.data == b.data  # 无文案时行为与纯照片一致


# ---------- 每日精选 ----------

def _seed_scores():
    # 构造两张评分照片：一张拍摄于"历史上的今天"（假设今天 8 月 3 日附近）
    today = dt.date.today()
    p1 = _make_photo(os.path.join(_tmp, "daily1.png"))
    p2 = _make_photo(os.path.join(_tmp, "daily2.png"))
    shot = dt.datetime(today.year - 2, today.month, today.day, 12, 0, 0)
    db.upsert_photo_score(p1, filename="daily1.png", memory_score=90,
                          shot_at=shot.timestamp(), shot_source="exif", analyzed_at=db.now())
    db.upsert_photo_score(p2, filename="daily2.png", memory_score=40,
                          shot_at=shot.timestamp(), shot_source="exif", analyzed_at=db.now())
    return p1, p2


def test_select_daily_photo_historical_today(monkeypatch):
    _seed_scores()
    monkeypatch.setattr("app.daily.settings.daily_min_score", 50)
    chosen, is_today = daily.select_daily_photo()
    assert chosen is not None
    assert chosen["memory_score"] >= 50  # 高于阈值的候选
    assert chosen["filename"] == "daily1.png"
    assert is_today is True


def test_select_daily_falls_back_to_high_score(monkeypatch):
    """无历史上的今天候选时，退化为全局高分（is_today=False，渲染不展示日期）。"""
    p = _make_photo(os.path.join(_tmp, "high.png"))
    # 拍摄日期故意设为"非今天"
    shot = dt.datetime(2020, 1, 1, 12, 0, 0)
    db.upsert_photo_score(p, filename="high.png", memory_score=95,
                          shot_at=shot.timestamp(), shot_source="exif", analyzed_at=db.now())
    monkeypatch.setattr("app.daily.settings.daily_min_score", 99)  # 阈值过高 → 无历史候选
    chosen, is_today = daily.select_daily_photo()
    assert chosen is not None
    assert is_today is False


def test_render_daily_creates_file():
    _seed_scores()
    meta = daily.render_daily()
    assert meta is not None
    assert daily.daily_fps6_exists()
    assert daily.daily_is_fresh()
    assert meta["id"].startswith("daily-")  # 内容指纹 id：文件变化时设备可感知
    assert meta["width"] == 1200
