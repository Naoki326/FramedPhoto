"""pytest 配置：统一把 db / upload / ota 隔离到临时目录。

必须在任何 `import app...` 之前执行，否则 settings 会读取默认路径，
测试可能污染开发数据（如 daily 渲染写进 services/daily）。
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(__file__) + "/..")

_tmp = tempfile.mkdtemp(prefix="framedphoto-pytest-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["UPLOAD_DIR"] = os.path.join(_tmp, "uploads")
os.environ["OTA_DIR"] = os.path.join(_tmp, "ota")
# 校准 profile 同样隔离，避免测试调用 save_calibrated 覆盖真实校准数据
os.environ["FRAMEDPHOTO_CALIBRATED_PROFILE"] = os.path.join(_tmp, "calibrated.json")


@pytest.fixture(autouse=True)
def _isolate_render_cache(monkeypatch, tmp_path):
    """把 free_module / weather_card 的磁盘缓存隔离到临时目录。

    test_api 等集成测试会经 /content 触发真实渲染，若直接写
    app/free_cache 会污染开发数据（如测试生成的纯文字卡被服务命中）。
    """
    from app import free_module, weather_card
    monkeypatch.setattr(free_module, "CACHE_DIR", tmp_path / "free_cache")
    monkeypatch.setattr(weather_card, "CACHE_DIR", tmp_path / "weather_cache")
    monkeypatch.setattr(weather_card, "_IP_LOC_FILE", tmp_path / "weather_cache" / "ip_loc.json")
