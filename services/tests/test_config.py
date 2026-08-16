"""config 公开设置：渲染缓存路径（#20）。

自由模块渲染缓存目录、天气卡片渲染缓存目录与 IP 定位缓存文件路径
升为 config 公开设置：env 可覆盖、默认值与私有路径时代一致；
conftest 的渲染缓存隔离改走这些公开设置（同 DB_PATH 模式）。
"""
import json
import time
from pathlib import Path

from app.config import Settings, settings

# 默认值锚点：私有路径时代的落点 = 仓库内 services/app/ 下的两个缓存目录
# （tests/ 的上一级是 services/，与 app/ 平级——独立于 config.py 的实现推导）。
_APP_DIR = Path(__file__).resolve().parents[1] / "app"
_CACHE_ENV_KEYS = ("FREE_CACHE_DIR", "WEATHER_CACHE_DIR", "IP_LOC_CACHE_FILE")


def _fresh_settings(monkeypatch, **env) -> Settings:
    """清掉 conftest 注入的隔离 env 后按给定 env 构造 Settings。"""
    for key in _CACHE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings()


# ---------- 公开设置本身：默认值 / env 覆盖 ----------

def test_render_cache_defaults_match_previous_private_locations(monkeypatch):
    s = _fresh_settings(monkeypatch)
    assert Path(s.free_cache_dir) == _APP_DIR / "free_cache"
    assert Path(s.weather_cache_dir) == _APP_DIR / "weather_cache"
    assert Path(s.ip_loc_cache_file) == _APP_DIR / "weather_cache" / "ip_loc.json"


def test_render_cache_env_override(monkeypatch, tmp_path):
    s = _fresh_settings(
        monkeypatch,
        FREE_CACHE_DIR=str(tmp_path / "f"),
        WEATHER_CACHE_DIR=str(tmp_path / "w"),
        IP_LOC_CACHE_FILE=str(tmp_path / "elsewhere.json"),
    )
    assert Path(s.free_cache_dir) == tmp_path / "f"
    assert Path(s.weather_cache_dir) == tmp_path / "w"
    assert Path(s.ip_loc_cache_file) == tmp_path / "elsewhere.json"


def test_ip_loc_file_defaults_into_weather_cache_dir(monkeypatch, tmp_path):
    """只改天气缓存目录时，IP 定位缓存随之落在该目录下（沿用原耦合默认）。"""
    s = _fresh_settings(monkeypatch, WEATHER_CACHE_DIR=str(tmp_path / "w"))
    assert Path(s.ip_loc_cache_file) == tmp_path / "w" / "ip_loc.json"


# ---------- 模块绑定：渲染缓存路径来自公开设置 ----------

def test_modules_bind_cache_paths_from_public_settings():
    """模块缓存路径绑定公开设置：conftest 经 env 隔离后不再指向仓库内目录。"""
    from app import free_module, weather_card
    assert free_module.CACHE_DIR == Path(settings.free_cache_dir)
    assert weather_card.CACHE_DIR == Path(settings.weather_cache_dir)
    assert _APP_DIR not in free_module.CACHE_DIR.parents
    assert _APP_DIR not in weather_card.CACHE_DIR.parents


def test_ip_location_reads_disk_cache_at_configured_file(monkeypatch):
    """IP 定位缓存文件来自公开设置：磁盘缓存有效时 ip_location() 直接命中，
    不发起网络请求。"""
    from app import weather_card
    loc = {"city": "测试市", "lat": 31.2, "lon": 121.4}
    cache_file = Path(settings.ip_loc_cache_file)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"loc": loc, "ts": time.time()}), encoding="utf-8")

    monkeypatch.setattr(weather_card, "_IP_LOC_CACHE", None)   # 复位内存缓存
    monkeypatch.setattr(weather_card, "_IP_LOC_TS", 0)
    network_calls: list = []

    def _no_network(*_a, **_k):
        network_calls.append(1)
        raise AssertionError("应命中磁盘缓存，不应发起网络请求")

    monkeypatch.setattr("requests.get", _no_network)
    assert weather_card.ip_location() == loc
    assert not network_calls
