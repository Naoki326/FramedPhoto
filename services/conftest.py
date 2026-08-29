"""pytest 配置：统一把 db / upload / ota / 渲染缓存隔离到临时目录。

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
# 渲染缓存同样经公开设置隔离（同 DB_PATH 模式）：自由模块 / 天气卡片缓存
# 目录与 IP 定位缓存文件（#20，不再 monkeypatch 模块私有路径属性）
os.environ["FREE_CACHE_DIR"] = os.path.join(_tmp, "free_cache")
os.environ["WEATHER_CACHE_DIR"] = os.path.join(_tmp, "weather_cache")
os.environ["CALENDAR_CACHE_DIR"] = os.path.join(_tmp, "calendar_cache")
os.environ["IP_LOC_CACHE_FILE"] = os.path.join(_tmp, "weather_cache", "ip_loc.json")
# runtime_config 同样在 conftest 加载期（import 阶段）就重定向到临时文件：
# 即使某测试进程绕过 _isolate_runtime_config fixture（如直接 import app 后
# 调 runtime_config.save），也绝不会写回真实 services/app/runtime_config.json
# ——曾真实发生（#25 期间测试把 slot_segments 覆盖成单段 photo，8/25 14:10）。
os.environ["FRAMEDPHOTO_TEST_CONFIG"] = os.path.join(_tmp, "runtime_config.json")
# 测试不走真实 VLM：services/.env 里有真实 key，不关的话 API 集成测试
# （上传自动分析 / 每日精选文案）会真实调用外部 API——慢、花钱且不确定。
# 需要 VLM 的用例（test_ai_features）均自行 monkeypatch settings，不受影响。
os.environ["VLM_ENABLED"] = "false"
# 校准 profile 同样隔离，避免测试调用 save_calibrated 覆盖真实校准数据
os.environ["FRAMEDPHOTO_CALIBRATED_PROFILE"] = os.path.join(_tmp, "calibrated.json")


@pytest.fixture(autouse=True)
def _isolate_runtime_config(monkeypatch, tmp_path):
    """runtime_config 隔离到临时文件。

    否则测试既会读到真实服务配置（时段/手动指定今日精选等影响断言），
    也可能经 save() 写回真实配置（污染运行中的服务）。
    """
    from app import runtime_config
    monkeypatch.setattr(runtime_config, "CONFIG_PATH", tmp_path / "runtime_config.json")
